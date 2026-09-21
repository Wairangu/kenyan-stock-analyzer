# Custom-domain HTTPS front-end for the dashboard, at stocks.getkitters.com.
#
# getkitters.com is an EXISTING, LIVE domain in this account, already
# serving an unrelated product (apex/www -> CloudFront distribution
# E13YM3PL357GAY, admin.getkitters.com, api.getkitters.com,
# mara.getkitters.com -- all pre-existing, none of it touched here).
# Everything below is a brand-new subdomain, certificate, OAC, and
# distribution dedicated to this app -- purely additive.
#
# Matches the account's existing CloudFront convention (see
# E13YM3PL357GAY): private S3 bucket + Origin Access Control, not a
# public S3-website-hosting bucket.

data "aws_route53_zone" "getkitters" {
  name         = "getkitters.com."
  private_zone = false
}

# ---- ACM certificate (must be us-east-1 for CloudFront) ----

resource "aws_acm_certificate" "dashboard" {
  provider          = aws.us_east_1
  domain_name       = "stocks.getkitters.com"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "dashboard_cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.dashboard.domain_validation_options : dvo.domain_name => {
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

resource "aws_acm_certificate_validation" "dashboard" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.dashboard.arn
  validation_record_fqdns = [for r in aws_route53_record.dashboard_cert_validation : r.fqdn]
}

# ---- CloudFront (private S3 origin via Origin Access Control) ----

resource "aws_cloudfront_origin_access_control" "reports" {
  name                              = "${var.project_name}-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "dashboard" {
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100" # matches the account's existing distribution
  aliases             = ["stocks.getkitters.com"]

  origin {
    domain_name              = aws_s3_bucket.reports.bucket_regional_domain_name
    origin_id                = "s3-${aws_s3_bucket.reports.id}"
    origin_access_control_id = aws_cloudfront_origin_access_control.reports.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-${aws_s3_bucket.reports.id}"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    min_ttl                = 0
    default_ttl            = 86400
    max_ttl                = 31536000

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.dashboard.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

resource "aws_route53_record" "dashboard" {
  zone_id = data.aws_route53_zone.getkitters.zone_id
  name    = "stocks.getkitters.com"
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.dashboard.domain_name
    zone_id                = "Z2FDTNDATAQYW2" # fixed, global CloudFront hosted-zone ID
    evaluate_target_health = false
  }
}
