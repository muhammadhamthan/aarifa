variable "name" {
  description = "Bucket name prefix, e.g. kx-pack-prod. The account id is appended for global uniqueness."
  type        = string
}

variable "buckets" {
  description = <<-EOT
    Buckets to create. Retention follows T20: call audio 90 days; exports are short-lived presigned
    downloads; audit exports use Object Lock (optional).
  EOT
  type = map(object({
    expiration_days            = optional(number)
    noncurrent_expiration_days = optional(number, 30)
    versioning                 = optional(bool, true)
    object_lock_days           = optional(number)
  }))
  default = {
    media   = {}                                                       # Payload CMS uploads, product images
    quotes  = {}                                                       # frozen quote PDFs + requirement summaries
    audio   = { expiration_days = 90, noncurrent_expiration_days = 1 } # call / voice recordings (T20)
    exports = { expiration_days = 7, versioning = false }              # XLSX/CSV/PDF report exports
  }
}

variable "kms_key_arn" {
  type = string
}

variable "force_destroy" {
  description = "Allow terraform destroy of non-empty buckets (staging only)."
  type        = bool
  default     = false
}

variable "enable_media_cdn" {
  description = "CloudFront distribution in front of the media bucket (OFF: website/CRM on Vercel call the UAE API directly). Note: CloudFront edge caches are global."
  type        = bool
  default     = false
}

variable "cdn_bucket" {
  type    = string
  default = "media"
}

variable "cors_allowed_origins" {
  description = "Origins allowed to PUT via presigned URLs (CRM on Vercel)."
  type        = list(string)
  default     = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
