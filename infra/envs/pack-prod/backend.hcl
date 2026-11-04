# terraform init -backend-config=backend.hcl
bucket       = "kx-tfstate-pack-prod-REPLACE_ACCOUNT_ID"
key          = "envs/pack-prod/terraform.tfstate"
region       = "me-central-1"
encrypt      = true
use_lockfile = true
