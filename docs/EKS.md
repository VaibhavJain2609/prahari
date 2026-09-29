# EKS central plane

The platform runs on EKS in `ap-south-1` instead of k3d on the laptop. The
reason for the move is laptop disk, which is why the image pipeline matters as
much as the cluster: **GitHub Actions builds all images and pushes them to
ECR** — the laptop never stores a torch/CUDA layer.

The `district` Terraform module (k3s on EC2 GPU nodes) is unchanged: that is
the *edge* plane, deliberately separate. This env is the *central* plane.
Its VPC CIDR is `10.255.0.0/16`, the value the district module's
`central_plane_cidr` already defaults to — pointing a district at this cluster
is a tfvars line, not new code.

## What exists where

| Piece | Where | File |
|---|---|---|
| Cluster, VPC, node group, addons, ECR, GHA OIDC role | Terraform | `infra/terraform/envs/eks/` |
| EKS values profile | Helm | `infra/helm/prahari/values-eks.yaml` |
| gp3 StorageClass + storageClassName wiring | Helm | `templates/storageclass.yaml`, PVCs |
| Media-only public LB (8554/8888/8889 — never 9997/9998) | Helm | `mediamtx.publicLb` → `prahari-mediamtx-public` Service |
| Image build+push+deploy | GitHub Actions | `.github/workflows/images-ecr.yml` (push → deploy job) |
| Shared deploy path | shell | `scripts/eks-deploy.sh` — used by Make AND the CD job |
| DNS sync | boto3 | `scripts/eks_dns_sync.py` — CNAME upserts to live LB hostnames |
| Route53 zone + ACM cert | Terraform | `domain_name` var in `infra/terraform/envs/eks/` |
| Deploy/secret targets | Make | `eks-kubeconfig`, `eks-secrets`, `eks-up`, `eks-dns`, `eks-images`, `eks-down` |

## Bring-up

1. `aws login` (SSO). The API endpoint is public (`cluster_public_access_cidrs`
   defaults to `0.0.0.0/0`) because GitHub-hosted runners deploy here — it's
   TLS + IAM/OIDC authenticated. Optionally set `domain_name` to a subdomain
   you control (see DNS below).
2. `terraform -chdir=infra/terraform/envs/eks init && apply` — ~15 min for EKS.
3. Set GitHub repo variables from terraform outputs:
   `AWS_ECR_PUSH_ROLE_ARN`, `AWS_EKS_DEPLOY_ROLE_ARN`, `AWS_REGION` —
   plus `PRAHARI_DOMAIN` if a domain is configured.
4. Merge to main → the `images-ecr` workflow builds, pushes to ECR, and
   deploys: the `deploy` job assumes a second OIDC role (`gha-deploy`,
   scoped to this cluster), runs `scripts/eks-deploy.sh` — the same script
   `make eks-up` runs locally — and deploys the `sha-<commit>` tag built in
   that run.
5. `make eks-kubeconfig && make eks-secrets` — one-time, on the first deploy
   (the Secrets are out-of-band by design; CI cannot mint them).

## DNS (optional, Namecheap or any registrar)

Terraform only creates a Route53 zone for a **subdomain** you delegate —
e.g. `domain_name = "eks.example.com"`. The one manual change at the
registrar:

```
eks  NS  <the 4 name servers from `terraform output hosted_zone_name_servers`>
```

After delegation, everything is automatic: the ACM wildcard cert
(`*.eks.example.com`) validates via records Terraform already wrote into the
zone, and each deploy runs `scripts/eks_dns_sync.py` (boto3) which upserts:

- `console.<domain>` CNAME → prahari-web NLB
- `streams.<domain>` CNAME → prahari-mediamtx-public NLB

Once the cert shows ISSUED, `eks-deploy.sh` annotates both NLB Services with
`aws-load-balancer-ssl-cert` + a 443 listener: console on
`https://console.<domain>`, WHEP on `https://streams.<domain>`, and cookies
flip back to Secure. Until then it deploys plain HTTP and says so. If you do
not want delegation, the alternative is hand-managed CNAMEs at Namecheap —
but then ACM validation records must also be created by hand, every renew.

Console without DNS: `http://<prahari-web NLB hostname>:3000`. WHEP previews:
`http://<prahari-mediamtx-public hostname>:8889`.

## Cost shape (ap-south-1, on-demand)

- EKS control plane: ~$73/mo, always on.
- 2× t3.large nodes: ~$50/mo.
- 2 NLBs (web + mediamtx-public): ~$32/mo + data.
- EBS gp3 (postgres 20Gi + audit 1Gi): ~$2/mo.
- ECR storage: cents at this size (lifecycle keeps last 15).
- Rough total: **~$160/mo** while running. `terraform destroy` kills the
  nodes+LBs; the control plane charge stops only when the cluster is deleted.

## Follow-ups (deliberately not built)

- **HTTP→HTTPS.** Plain listeners stay open alongside :443 — NLB is L4, it
  cannot redirect. Tighten by removing the plain web/WHEP ports once TLS is
  live, or leave them for in-VPC consumers.
- **GPU node group.** `g6.xlarge` + `runtimeClassName: nvidia` + the
  values-gpu.yaml layering (`-f values-eks.yaml -f values-gpu.yaml`). Costs
  ~$500/mo per node — add it for measurement runs, not the daily plane.
- **NetworkPolicy enforcement.** Rendered policies are correct (postgres now
  admits all four `*_DATABASE_URL` holders) but vpc-cni doesn't enforce them
  until its network-policy agent is enabled — verify kubelet probe behaviour
  before flipping it on.
- **Private subnets + NAT, S3 terraform state, external IdP, KEDA install.**

## The escape hatch

`make eks-images` builds + pushes from the laptop when CI is broken. It costs
local disk every time — the reason the GHA path exists.
