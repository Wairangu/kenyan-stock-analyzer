# Private bucket the Lambda uploads the HTML dashboard into. Served
# publicly only via CloudFront + Origin Access Control (see
# cloudfront.tf) at https://stocks.getkitters.com -- the bucket itself
# grants no direct public access.

resource "aws_s3_bucket" "reports" {
  bucket = "${var.project_name}-reports-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
}

# Explicitly block all public access -- this bucket is reachable only via
# CloudFront (see the policy below), never directly. Explicit rather than
# relying on inherited/account-default behavior, since this bucket
# previously had public access explicitly enabled and we want no
# ambiguity about the new state.
resource "aws_s3_bucket_public_access_block" "reports" {
  bucket = aws_s3_bucket.reports.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "reports_cloudfront_oac" {
  bucket = aws_s3_bucket.reports.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontServicePrincipalReadOnly"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.reports.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.dashboard.arn
          }
        }
      }
    ]
  })
}

# The bucket is overwritten daily; expire old dated filenames
# (market_summary_<timestamp>.html) so they don't accumulate forever.
resource "aws_s3_bucket_lifecycle_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id

  rule {
    id     = "expire-old-reports"
    status = "Enabled"

    filter {}

    expiration {
      days = 90
    }
  }
}
