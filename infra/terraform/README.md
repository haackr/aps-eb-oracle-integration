# Terraform: Cloud Run Jobs + Cloud Scheduler

This Terraform stack deploys three scheduled integration jobs:

- `requisitions_export`: e-Builder -> Oracle FTP
- `req_import`: Oracle FTP `/outbound/REQ` -> e-Builder Upload
- `po_import`: Oracle FTP `/outbound/PO` -> e-Builder Upload

## What it creates

- Two service accounts:
  - Runtime service account for Cloud Run Jobs
  - Scheduler service account for Cloud Scheduler HTTP auth
- Three `google_cloud_run_v2_job` resources
- Three `google_cloud_scheduler_job` resources
- IAM bindings:
  - Scheduler SA can run Cloud Run Jobs
  - Runtime SA can access configured Secret Manager secrets
- Optional API enablement for required GCP services

## Usage

1. Copy the variables file:

```bash
cp terraform.tfvars.example terraform.tfvars
```

2. Fill in:

- `project_id`
- `container_image`
- secret names in `secret_env_vars`
- any schedule/env overrides you want

3. Initialize/apply:

```bash
terraform init
terraform plan
terraform apply
```

## Secret Manager expectations

`secret_env_vars` and `per_job_secret_env_vars` values must be existing Secret Manager secret names in the target project.

## Deploying your image

This stack assumes the image already exists in Artifact Registry. Build/push your container before applying.
