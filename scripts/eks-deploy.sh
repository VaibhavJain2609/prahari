#!/usr/bin/env bash
# The ONE EKS deploy path. `make eks-up` calls this locally; the images-ecr
# workflow's deploy job calls it on the runner. Two callers, one code path —
# a deploy step that exists twice is a deploy step that silently diverges.
#
# Required env:
#   IMAGE_REGISTRY   ECR registry host (e.g. 123456789012.dkr.ecr.ap-south-1.amazonaws.com)
# Optional env:
#   IMAGE_TAG        default "latest" — CI passes sha-<commit>
#   NAMESPACE        default "prahari"
#   CHART            default infra/helm/prahari
#   PRAHARI_DOMAIN   the delegated zone (e.g. eks.example.com). When set AND an
#                    ISSUED ACM cert exists for *.$PRAHARI_DOMAIN, the NLBs get
#                    TLS listeners and cookies go Secure. When the cert is not
#                    yet issued (NS delegation pending), the deploy continues
#                    on plain HTTP — loudly, not silently.
set -euo pipefail

CHART="${CHART:-infra/helm/prahari}"
NAMESPACE="${NAMESPACE:-prahari}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
: "${IMAGE_REGISTRY:?set IMAGE_REGISTRY to the ECR registry host}"

SETS=(
  --set "profile=eks"
  --set "global.imageRegistry=${IMAGE_REGISTRY}"
  --set "global.imageTag=${IMAGE_TAG}"
)

ACM_ARN=""
if [ -n "${PRAHARI_DOMAIN:-}" ]; then
  # Look the cert up rather than trusting an ARN in state: a cert that exists
  # but is not yet ISSUED must not be annotated onto a listener — the NLB
  # would provision a TLS listener that can never serve.
  ACM_ARN="$(aws acm list-certificates --certificate-statuses ISSUED \
    --query "CertificateSummaryList[?DomainName=='*.${PRAHARI_DOMAIN}'].CertificateArn | [0]" \
    --output text 2>/dev/null || true)"
  [ "$ACM_ARN" = "None" ] && ACM_ARN=""
fi

if [ -n "$ACM_ARN" ]; then
  CONSOLE_HOST="console.${PRAHARI_DOMAIN}"
  STREAMS_HOST="streams.${PRAHARI_DOMAIN}"
  SETS+=(
    --set "tls.enabled=true"
    --set "tls.acmArn=${ACM_ARN}"
    --set "tls.consoleHost=${CONSOLE_HOST}"
    --set "tls.streamsHost=${STREAMS_HOST}"
    --set "mediamtx.browserWhepBase=https://${STREAMS_HOST}"
    --set "services.bff.sessionCookieSecure=true"
  )
  echo "tls: enabled — ACM cert ${ACM_ARN##*/}, console=${CONSOLE_HOST} streams=${STREAMS_HOST}"
else
  [ -n "${PRAHARI_DOMAIN:-}" ] && echo "tls: PRAHARI_DOMAIN set but no ISSUED cert for *.${PRAHARI_DOMAIN} — plain HTTP this deploy (delegate the NS records?)"
  # browserWhepBase is the BROWSER's view of WHEP — the mediamtx-public LB
  # hostname, which only exists after the first install. Looked up live and
  # injected when present: first run brings the stack up with previews
  # broken-but-loud, re-running repairs them once the LB lands.
  WHEP_HOST="$(kubectl -n "$NAMESPACE" get svc prahari-mediamtx-public \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  if [ -n "$WHEP_HOST" ]; then
    SETS+=(--set "mediamtx.browserWhepBase=http://${WHEP_HOST}:8889")
  else
    echo "note: prahari-mediamtx-public has no hostname yet — WHEP base not set this run"
  fi
  SETS+=(--set "services.bff.sessionCookieSecure=false")
fi

helm upgrade --install prahari "$CHART" \
  --namespace "$NAMESPACE" --create-namespace \
  --values "$CHART/values-eks.yaml" \
  "${SETS[@]}" \
  --wait --timeout 5m

echo "--- public endpoints ---"
kubectl -n "$NAMESPACE" get svc prahari-web prahari-mediamtx-public \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.loadBalancer.ingress[0].hostname}{"\n"}{end}'
