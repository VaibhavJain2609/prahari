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
| Image build+push | GitHub Actions | `.github/workflows/images-ecr.yml` |
| Deploy/secret targets | Make | `eks-kubeconfig`, `eks-secrets`, `eks-up`, `eks-images`, `eks-down` |

## Bring-up

1. `aws login` (SSO) — the API endpoint is scoped to `operator_cidr`, set it
   in `infra/terraform/envs/eks/terraform.tfvars`.
2. `terraform -chdir=infra/terraform/envs/eks init && apply` — ~15 min for EKS.
3. Set GitHub repo variables `AWS_ECR_PUSH_ROLE_ARN` (terraform output
   `github_actions_role_arn`) and `AWS_REGION=ap-south-1`.
4. Merge to main → the `images-ecr` workflow pushes all images to ECR
   (`:latest` + `:sha-<commit>`).
5. `make eks-kubeconfig` — kubectl context now points at EKS.
6. `make eks-secrets` — gateway, `prahari-internal`, bootstrap admin.
7. `make eks-up` — helm install profile=eks, ECR registry injected from
   terraform output. Re-run once `prahari-mediamtx-public` has an LB hostname
   so `browserWhepBase` lands.

Console: `http://<prahari-web NLB hostname>:3000`. WHEP previews:
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

- **TLS/domain.** Everything is plain HTTP behind NLBs — `sessionCookieSecure`
  is explicitly `"false"` in values-eks. ACM + ingress (or CloudFront) and a
  real hostname removes that.
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
