# SES sandbox mode is sufficient here: every address ever sent to/from is
# verified, so no production-access request is needed.
#
# ses_sender/ses_recipient are set in terraform.tfvars (gitignored, not
# committed) to a real address already verified in this account -- so by
# default NEITHER identity is created/managed here, and no manual "click
# the verification link" step is needed at all.
#
# If you point ses_sender/ses_recipient at a *new*, not-yet-verified
# address, set the corresponding *_already_verified variable to false so
# Terraform creates that identity. AWS then sends a verification email as
# a side effect of creation -- Terraform can't click that link for you, so
# check the inbox and confirm (`aws ses get-identity-verification-attributes`)
# before the Lambda's first real (non-DRY_RUN) invocation, or SES will
# reject the send at runtime.

resource "aws_ses_email_identity" "sender" {
  count = var.ses_sender_already_verified ? 0 : 1
  email = var.ses_sender
}

resource "aws_ses_email_identity" "recipient" {
  count = (var.ses_recipient == var.ses_sender || var.ses_recipient_already_verified) ? 0 : 1
  email = var.ses_recipient
}

# SES identity ARNs are deterministic, so we can reference them for IAM
# scoping whether or not Terraform actually created/owns those resources
# above.
#
# Important: for a DOMAIN-verified identity (e.g. getkitters.com), the
# correct IAM resource ARN is identity/<domain> -- NOT
# identity/<local-part>@<domain>. ses_sender_identity/ses_recipient_identity
# let the domain case be expressed explicitly; when left blank (the
# email-address-identity case, e.g. a plain Gmail address), each falls back
# to ses_sender/ses_recipient itself, preserving the original behavior.
#
# Confirmed by a real send failing with AccessDenied: SES's resource-level
# authorization for ses:SendEmail checks BOTH the sender's AND the
# recipient's identity ARN (relevant in sandbox mode, where the
# destination must also be an authorized identity) -- granting permission
# on only the sender's ARN is not sufficient.
locals {
  ses_sender_identity_name    = var.ses_sender_identity != "" ? var.ses_sender_identity : var.ses_sender
  ses_recipient_identity_name = var.ses_recipient_identity != "" ? var.ses_recipient_identity : var.ses_recipient
  ses_sender_arn              = "arn:aws:ses:${var.aws_region}:${data.aws_caller_identity.current.account_id}:identity/${local.ses_sender_identity_name}"
  ses_recipient_arn           = "arn:aws:ses:${var.aws_region}:${data.aws_caller_identity.current.account_id}:identity/${local.ses_recipient_identity_name}"
}
