resource "aws_iam_role" "lambda_exec" {
  name = "${var.project_name}-lambda-role"

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

resource "aws_iam_role_policy" "lambda_exec" {
  name = "${var.project_name}-lambda-policy"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "UploadDashboard"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.reports.arn}/*"
      },
      {
        # Read back the accumulated history/<date>.json signal snapshots
        # (src/signal_history.py) to compute the tv_class track record in
        # src/track_record.py, and the market_summary_*.html archive
        # (src/report_archive.py) to compute the Track Record page's older
        # bullish/bearish signal from the ~7 weeks of it that predate
        # history/ -- the Lambda has only ever needed to write before now.
        Sid    = "ReadSignalHistory"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.reports.arn}/history/*",
          "${aws_s3_bucket.reports.arn}/market_summary_*",
        ]
      },
      {
        Sid      = "ListSignalHistory"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.reports.arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["history/*", "market_summary_*"]
          }
        }
      },
      {
        Sid      = "SendSummaryEmail"
        Effect   = "Allow"
        Action   = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = distinct([local.ses_sender_arn, local.ses_recipient_arn])
      },
      {
        Sid    = "WriteLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project_name}*:*"
      },
      {
        Sid      = "InvalidateDashboardCache"
        Effect   = "Allow"
        Action   = ["cloudfront:CreateInvalidation"]
        Resource = aws_cloudfront_distribution.dashboard.arn
      },
      {
        # Table extraction for the government bond prices section (see
        # src/bond_data.py) -- AnalyzeDocument has no resource-level ARN
        # to scope to, "*" is the only valid Resource for this action.
        Sid      = "AnalyzeBondPricesPdf"
        Effect   = "Allow"
        Action   = ["textract:AnalyzeDocument"]
        Resource = "*"
      }
    ]
  })
}
