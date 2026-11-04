# terraform init -backend-config=backend.hcl
bucket       = "kx-tfstate-staging-REPLACE_ACCOUNT_ID"
key          = "envs/staging/terraform.tfstate"
region       = "me-central-1"
encrypt      = true
use_lockfile = true
