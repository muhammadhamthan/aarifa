# terraform init -backend-config=backend.hcl
bucket       = "kx-tfstate-auto-prod-REPLACE_ACCOUNT_ID"
key          = "envs/auto-prod/terraform.tfstate"
region       = "me-central-1"
encrypt      = true
use_lockfile = true
