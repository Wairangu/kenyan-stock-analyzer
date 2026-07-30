resource "aws_lambda_function" "analyzer" {
  function_name = var.project_name
  role          = aws_iam_role.lambda_exec.arn

  package_type = "Image"
  image_uri    = "${aws_ecr_repository.lambda.repository_url}:${var.image_tag}"

  memory_size = var.lambda_memory_mb
  timeout     = var.lambda_timeout_seconds
  # Default (512 MB) ephemeral storage is far more than enough -- a
  # measured local run's reports/ + data/ output totals well under 1 MB.

  environment {
    variables = {
      CACHE_DIR                  = "/tmp/data"
      REPORT_DIRECTORY           = "/tmp/reports"
      LOG_FILE                   = "/tmp/logs/analyzer.log"
      DATA_SOURCES               = "tradingview,yahoo_finance"
      STOCK_SYMBOLS              = var.stock_symbols
      ENABLE_EMAIL_NOTIFICATIONS = "false" # we send via SES ourselves, not main.py's SMTP path
      DRY_RUN                    = "false"
      S3_BUCKET                  = aws_s3_bucket.reports.id
      SES_SENDER                 = var.ses_sender
      SES_RECIPIENT              = var.ses_recipient
      DASHBOARD_URL              = "https://stocks.getkitters.com"
      CLOUDFRONT_DISTRIBUTION_ID = aws_cloudfront_distribution.dashboard.id
    }
  }

  # No dependency on aws_ses_email_identity.sender: the IAM policy is
  # scoped via the deterministic local.ses_sender_arn (not a resource
  # attribute), and by default that identity isn't created at all since
  # it's already verified -- SES verification is a runtime concern for the
  # first real send, not a Terraform apply-ordering concern.
  depends_on = [
    aws_iam_role_policy.lambda_exec,
  ]
}

resource "aws_cloudwatch_log_group" "analyzer" {
  name              = "/aws/lambda/${var.project_name}"
  retention_in_days = 30
}
