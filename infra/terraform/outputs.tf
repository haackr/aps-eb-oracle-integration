output "cloud_run_job_names" {
  description = "Cloud Run Job names by job key."
  value = {
    for k, v in google_cloud_run_v2_job.jobs :
    k => v.name
  }
}

output "scheduler_job_names" {
  description = "Cloud Scheduler job names by job key."
  value = {
    for k, v in google_cloud_scheduler_job.triggers :
    k => v.name
  }
}

output "runtime_service_account_email" {
  description = "Service account used by Cloud Run Jobs."
  value       = google_service_account.runtime.email
}

output "scheduler_service_account_email" {
  description = "Service account used by Cloud Scheduler HTTP trigger auth."
  value       = google_service_account.scheduler.email
}
