variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run Jobs and Scheduler."
  type        = string
  default     = "us-central1"
}

variable "name_prefix" {
  description = "Prefix for created resource names."
  type        = string
  default     = "aps-eb-oracle"
}

variable "container_image" {
  description = "Container image URI used by all Cloud Run Jobs."
  type        = string
}

variable "scheduler_time_zone" {
  description = "Time zone used by Cloud Scheduler cron expressions."
  type        = string
  default     = "America/Chicago"
}

variable "job_schedules" {
  description = "Cron schedules for each job key."
  type        = map(string)
  default = {
    requisitions_export = "50 15 * * 1-5"
    invoices_export     = "50 16 * * 1-5"
    po_changes_export   = "35 16 * * 1-5"
    req_import          = "20 16 * * 1-5"
    po_import           = "35 16 * * 1-5"
    ap_invoice_import   = "20 17 * * 1-5"
  }
}

variable "job_timeout_seconds" {
  description = "Timeout for each Cloud Run Job execution."
  type        = number
  default     = 900
}

variable "job_cpu" {
  description = "CPU limit per Cloud Run Job task."
  type        = string
  default     = "1"
}

variable "job_memory" {
  description = "Memory limit per Cloud Run Job task."
  type        = string
  default     = "512Mi"
}

variable "plain_env_vars" {
  description = "Non-secret env vars applied to all jobs."
  type        = map(string)
  default     = {}
}

variable "per_job_plain_env_vars" {
  description = "Per-job non-secret env vars merged with plain_env_vars."
  type        = map(map(string))
  default     = {}
}

variable "secret_env_vars" {
  description = "Map env var name -> Secret Manager secret name (common to all jobs)."
  type        = map(string)
  default     = {}
}

variable "per_job_secret_env_vars" {
  description = "Per-job map env var name -> Secret Manager secret name."
  type        = map(map(string))
  default     = {}
}

variable "enable_apis" {
  description = "Enable required GCP APIs in the target project."
  type        = bool
  default     = true
}
