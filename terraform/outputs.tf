output "dashboard_url" {
  description = "CloudFront-fronted custom domain serving the dashboard over HTTPS."
  value       = "https://${aws_route53_record.dashboard.name}"
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.dashboard.id
}

output "s3_bucket_name" {
  value = aws_s3_bucket.reports.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.lambda.repository_url
}

output "lambda_function_name" {
  value = aws_lambda_function.analyzer.function_name
}

output "lambda_function_arn" {
  value = aws_lambda_function.analyzer.arn
}

output "ses_verification_note" {
  description = "Whether Terraform created a new SES identity that still needs the verification email clicked, or the address(es) were already verified (no action needed)."
  value = (
    var.ses_sender_already_verified && (var.ses_recipient == var.ses_sender || var.ses_recipient_already_verified)
    ? (var.ses_sender == var.ses_recipient
      ? "${var.ses_sender} is an already-verified identity -- no action needed."
    : "Both ${var.ses_sender} and ${var.ses_recipient} are already-verified identities -- no action needed.")
    : "Check inbox(es) for a verification email, then confirm with: aws ses get-identity-verification-attributes --identities ${var.ses_sender} ${var.ses_recipient} --region ${var.aws_region}"
  )
}
