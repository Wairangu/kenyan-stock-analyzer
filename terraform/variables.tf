variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "eu-west-1"
}

variable "project_name" {
  description = "Prefix applied to every resource name, to namespace this app in a shared AWS account."
  type        = string
  default     = "kenyan-stock-analyzer"
}

variable "image_tag" {
  description = "ECR image tag the Lambda function should run. Push an image with this tag (see aws/build.sh) before applying, or the function will fail to update."
  type        = string
  default     = "latest"
}

variable "ses_sender" {
  description = "Verified SES sender address (the 'From' on the daily email). Set your real address in terraform.tfvars (gitignored) -- do not hardcode it here."
  type        = string
  default     = "you@example.com"
}

variable "ses_sender_already_verified" {
  description = "Set true when ses_sender is already a verified SES identity in this account, so Terraform does not create/manage -- and therefore does not delete on destroy -- an identity it doesn't own."
  type        = bool
  default     = true
}

variable "ses_sender_identity" {
  description = "SES identity name (domain or email) that authorizes ses_sender, used to build the IAM ARN for ses:SendEmail. Leave blank when ses_sender is itself an email-address-verified identity (ARN uses ses_sender directly). Set to just the domain (e.g. \"getkitters.com\") when ses_sender is an address under a domain-verified identity -- SES's IAM resource ARN for a domain identity is arn:...:identity/<domain>, not arn:...:identity/<local-part>@<domain>."
  type        = string
  default     = ""
}

variable "ses_recipient" {
  description = "Verified SES recipient address (who receives the daily email). Set your real address in terraform.tfvars (gitignored) -- do not hardcode it here."
  type        = string
  default     = "you@example.com"
}

variable "ses_recipient_already_verified" {
  description = "Set true when ses_recipient is already a verified SES identity in this account, so Terraform does not create/manage -- and therefore does not delete on destroy -- an identity it doesn't own."
  type        = bool
  default     = true
}

variable "ses_recipient_identity" {
  description = "SES identity name (domain or email) that authorizes ses_recipient, used to build the IAM ARN needed for ses:SendEmail (SES's resource-level check covers the recipient identity too, not just the sender -- relevant in sandbox mode). Leave blank when ses_recipient is itself an email-address-verified identity."
  type        = string
  default     = ""
}

variable "stock_symbols" {
  description = "Comma-separated NSE watchlist symbols the Lambda analyzes."
  type        = string
  default     = "SCOM,EQTY,KCB,EABL,COOP,ABSA,NCBA,SCBK,IMH,KPLC"
}

variable "schedule_expression" {
  description = "EventBridge cron expression for the trigger. Default: 06:00 UTC (09:00 EAT, NSE market open), Mon-Fri."
  type        = string
  default     = "cron(0 6 ? * MON-FRI *)"
}

variable "lambda_memory_mb" {
  description = "Lambda memory in MB. Also determines allocated CPU. A local RIE test at 3008MB completed the full pipeline in ~145s."
  type        = number
  default     = 3008
}

variable "lambda_timeout_seconds" {
  description = "Lambda timeout in seconds. Measured local runtime was ~145s; this leaves headroom for cold start and real-network variance from eu-west-1 to Nairobi/TradingView/Google News sources."
  type        = number
  default     = 420
}
