# Password-protected personal trade journal at portfolio.getkitters.com.
#
# Separate from the public stocks.getkitters.com dashboard: trades.json
# holds genuinely sensitive personal financial data, so this bucket is
# fully private (Lambda-IAM-only access, never public/CloudFront-served
# as static content) and the app itself is gated by a login form + signed
# session cookie. Reuses data.aws_route53_zone.getkitters already
# declared in cloudfront.tf -- do not redeclare it here.
#
# The Lambda needs zero third-party pip dependencies: it reads current
# prices from the existing dashboard's public prices.json instead of
# calling TradingView itself, so it's a plain zip deploy (no ECR/Docker).

# ---- Private S3 bucket for trades.json ----

resource "aws_s3_bucket" "portfolio_trades" {
  bucket = "kenyan-stock-portfolio-trades-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
}

resource "aws_s3_bucket_public_access_block" "portfolio_trades" {
  bucket = aws_s3_bucket.portfolio_trades.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---- Secrets (SSM Parameter Store, SecureString) ----

resource "random_password" "portfolio_session_secret" {
  length  = 64
  special = true
}

# Shared secret CloudFront injects as a custom origin header on every
# request to the Lambda Function URL; the Lambda rejects anything that
# doesn't carry it. This (not IAM/OAC) is what restricts the Function URL
# to CloudFront-only traffic here -- see the long comment on
# aws_lambda_function_url.portfolio for why OAC's AWS_IAM signing model
# doesn't work for this app.
resource "random_password" "portfolio_origin_secret" {
  length  = 40
  special = false # kept alphanumeric -- this value travels as a raw HTTP header
}

resource "aws_ssm_parameter" "portfolio_username" {
  name  = "/kenyan-stock-portfolio/username"
  type  = "SecureString"
  value = var.portfolio_username
}

resource "aws_ssm_parameter" "portfolio_password_hash" {
  name  = "/kenyan-stock-portfolio/password_hash"
  type  = "SecureString"
  value = var.portfolio_password_hash

  # The Lambda rewrites this at runtime when the user changes their
  # password (see portfolio/lambda_handler.py's _set_password) -- once
  # created, Terraform must never overwrite that with the original
  # bootstrap value on a later apply.
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "portfolio_must_change_password" {
  name  = "/kenyan-stock-portfolio/must_change_password"
  type  = "String"
  value = "true" # forces a password change on first login of a fresh/reset credential

  lifecycle {
    ignore_changes = [value] # the Lambda flips this to "false" once the user sets a new password
  }
}

resource "aws_ssm_parameter" "portfolio_session_secret" {
  name  = "/kenyan-stock-portfolio/session_secret"
  type  = "SecureString"
  value = random_password.portfolio_session_secret.result
}

# ---- IAM (own role -- does not touch aws_iam_role.lambda_exec) ----

resource "aws_iam_role" "portfolio_lambda_exec" {
  name = "kenyan-stock-portfolio-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "portfolio_lambda_exec" {
  name = "kenyan-stock-portfolio-lambda-policy"
  role = aws_iam_role.portfolio_lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadWriteTrades"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.portfolio_trades.arn}/trades.json"
      },
      {
        Sid      = "ReadSecrets"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/kenyan-stock-portfolio/*"
      },
      {
        # Scoped to only the two parameters the app is allowed to rewrite
        # at runtime (a user changing their own password) -- username and
        # session_secret stay Terraform-only/read-only to this role.
        Sid    = "UpdateOwnCredentialState"
        Effect = "Allow"
        Action = ["ssm:PutParameter"]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/kenyan-stock-portfolio/password_hash",
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/kenyan-stock-portfolio/must_change_password",
        ]
      },
      {
        Sid    = "WriteLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/kenyan-stock-portfolio*:*"
      }
    ]
  })
}

# ---- Lambda (zip, not container -- zero pip dependencies) ----

data "archive_file" "portfolio" {
  type        = "zip"
  source_dir  = "${path.module}/../portfolio"
  output_path = "${path.module}/.build/portfolio.zip"
}

resource "aws_lambda_function" "portfolio" {
  function_name    = "kenyan-stock-portfolio"
  role             = aws_iam_role.portfolio_lambda_exec.arn
  package_type     = "Zip"
  filename         = data.archive_file.portfolio.output_path
  source_code_hash = data.archive_file.portfolio.output_base64sha256
  runtime          = "python3.13"
  handler          = "lambda_handler.handler"
  memory_size      = 256
  timeout          = 15

  environment {
    variables = {
      TRADES_BUCKET        = aws_s3_bucket.portfolio_trades.id
      TRADES_KEY           = "trades.json"
      PRICES_URL           = "https://stocks.getkitters.com/prices.json"
      SSM_PREFIX           = "/kenyan-stock-portfolio"
      ORIGIN_VERIFY_SECRET = random_password.portfolio_origin_secret.result
    }
  }

  depends_on = [aws_iam_role_policy.portfolio_lambda_exec]
}

resource "aws_cloudwatch_log_group" "portfolio" {
  name              = "/aws/lambda/kenyan-stock-portfolio"
  retention_in_days = 30
}

# ---- Function URL ----
#
# authorization_type = "NONE", not AWS_IAM/OAC. AWS's OAC + AWS_IAM model
# requires the ORIGINAL CLIENT to compute and send x-amz-content-sha256
# for any request with a body (POST/PUT) -- confirmed against AWS's own
# docs and by a live 403 InvalidSignatureException here -- which a plain
# browser <form> POST can never do (no client-side AWS credentials, no
# SigV4 signing in a vanilla form submission). That makes OAC incompatible
# with this app's login/add-trade forms.
#
# Instead this uses AWS's other documented pattern for restricting a
# Function URL to CloudFront-only traffic: CloudFront injects a secret
# header (custom_header below) that only it knows, and the Lambda rejects
# any request missing/mismatching it (see portfolio/lambda_handler.py's
# _origin_verified, checked first thing on every request). The Function
# URL itself is technically invokable directly, but any such request is
# immediately 403'd by the app before touching sessions/trades/S3.
resource "aws_lambda_function_url" "portfolio" {
  function_name      = aws_lambda_function.portfolio.function_name
  authorization_type = "NONE"
}

# authorization_type = "NONE" still requires an explicit resource policy
# granting invoke -- it only means requests don't need to be SigV4-signed,
# not that no permission is needed. Real access control is the app-level
# X-Origin-Verify header check, not this policy.
resource "aws_lambda_permission" "portfolio_function_url_public" {
  statement_id           = "AllowPublicInvokeFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.portfolio.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# AWS changed Function URL invocation requirements around October 2025:
# new Function URLs need BOTH lambda:InvokeFunctionUrl (above) AND
# lambda:InvokeFunction granted, independent of AuthType. This must be
# Principal "*" (like the statement above), NOT scoped to the
# cloudfront.amazonaws.com service principal: that scoping only means
# anything without OAC actually signing the request (which this app can't
# use -- see aws_lambda_function_url.portfolio's comment). Without OAC,
# CloudFront's proxied request to a custom origin carries no verifiable
# AWS service identity at all -- confirmed live: a cloudfront-scoped grant
# here never matched, CloudFront-routed requests got the identical 403 as
# a direct anonymous curl. Real access control is still the app-level
# X-Origin-Verify header check, not this policy.
resource "aws_lambda_permission" "portfolio_invoke_function_public" {
  statement_id  = "AllowPublicInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.portfolio.function_name
  principal     = "*"
}

# ---- ACM cert (DNS-validated, us-east-1 for CloudFront) ----

resource "aws_acm_certificate" "portfolio" {
  provider          = aws.us_east_1
  domain_name       = "portfolio.getkitters.com"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "portfolio_cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.portfolio.domain_validation_options : dvo.domain_name => {
      name  = dvo.resource_record_name
      type  = dvo.resource_record_type
      value = dvo.resource_record_value
    }
  }

  zone_id         = data.aws_route53_zone.getkitters.zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.value]
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "portfolio" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.portfolio.arn
  validation_record_fqdns = [for r in aws_route53_record.portfolio_cert_validation : r.fqdn]
}

# ---- CloudFront (Lambda Function URL origin, caching disabled) ----
#
# No Origin Access Control here -- see the comment on
# aws_lambda_function_url.portfolio for why OAC's AWS_IAM/SigV4 model is
# incompatible with this app's plain HTML form POSTs. CloudFront-only
# access is instead enforced via the custom_header secret below, checked
# by the Lambda itself.

resource "aws_cloudfront_distribution" "portfolio" {
  enabled     = true
  price_class = "PriceClass_100"
  aliases     = ["portfolio.getkitters.com"]

  origin {
    domain_name = replace(aws_lambda_function_url.portfolio.function_url, "/^https?:\\/\\/([^\\/]*)\\/?$/", "$1")
    origin_id   = "lambda-portfolio"

    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.portfolio_origin_secret.result
    }

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = "lambda-portfolio"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    min_ttl                = 0
    default_ttl            = 0
    max_ttl                = 0 # every response is dynamic/session-dependent -- never cache

    forwarded_values {
      query_string = true
      cookies {
        forward = "all" # the session cookie must reach the Lambda
      }
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.portfolio.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

resource "aws_route53_record" "portfolio" {
  zone_id = data.aws_route53_zone.getkitters.zone_id
  name    = "portfolio.getkitters.com"
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.portfolio.domain_name
    zone_id                = "Z2FDTNDATAQYW2" # fixed, global CloudFront hosted-zone ID
    evaluate_target_health = false
  }
}
