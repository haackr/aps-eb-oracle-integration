locals {
  required_apis = [
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
  ]

  jobs = {
    requisitions_export = {
      description = "e-Builder requisitions export to Oracle FTP"
      entrypoint  = "from integration_jobs import run_requisitions_export_job; result = run_requisitions_export_job(); print(result)"
      plain_env   = lookup(var.per_job_plain_env_vars, "requisitions_export", {})
      secret_env  = lookup(var.per_job_secret_env_vars, "requisitions_export", {})
    }
    req_import = {
      description = "Oracle FTP REQ import to e-Builder"
      entrypoint  = "from integration_jobs import run_req_numbers_import_job; result = run_req_numbers_import_job(); print(result)"
      plain_env   = lookup(var.per_job_plain_env_vars, "req_import", {})
      secret_env  = lookup(var.per_job_secret_env_vars, "req_import", {})
    }
    po_import = {
      description = "Oracle FTP PO import to e-Builder"
      entrypoint  = "from integration_jobs import run_po_numbers_import_job; result = run_po_numbers_import_job(); print(result)"
      plain_env   = lookup(var.per_job_plain_env_vars, "po_import", {})
      secret_env  = lookup(var.per_job_secret_env_vars, "po_import", {})
    }
  }

  all_secret_names = toset(flatten([
    values(var.secret_env_vars),
    flatten([for v in values(var.per_job_secret_env_vars) : values(v)])
  ]))
}

resource "google_project_service" "apis" {
  for_each = var.enable_apis ? toset(local.required_apis) : toset([])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "runtime" {
  account_id   = "${var.name_prefix}-runtime"
  display_name = "${var.name_prefix} runtime"
}

resource "google_service_account" "scheduler" {
  account_id   = "${var.name_prefix}-scheduler"
  display_name = "${var.name_prefix} scheduler"
}

resource "google_secret_manager_secret_iam_member" "runtime_secret_access" {
  for_each = local.all_secret_names

  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_job" "jobs" {
  for_each = local.jobs

  name                = "${var.name_prefix}-${replace(each.key, "_", "-")}"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.runtime.email
      timeout         = "${var.job_timeout_seconds}s"
      max_retries     = 1

      containers {
        image   = var.container_image
        command = ["python", "-c"]
        args    = [each.value.entrypoint]

        resources {
          limits = {
            cpu    = var.job_cpu
            memory = var.job_memory
          }
        }

        dynamic "env" {
          for_each = merge(var.plain_env_vars, each.value.plain_env)
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = merge(var.secret_env_vars, each.value.secret_env)
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  for_each = google_cloud_run_v2_job.jobs

  project  = var.project_id
  location = var.region
  name     = each.value.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "triggers" {
  for_each = local.jobs

  name        = "${var.name_prefix}-${replace(each.key, "_", "-")}-schedule"
  description = each.value.description
  schedule    = lookup(var.job_schedules, each.key)
  time_zone   = var.scheduler_time_zone
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.jobs[each.key].name}:run"

    headers = {
      "Content-Type" = "application/json"
    }

    body = base64encode("{}")

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_invoker]
}
