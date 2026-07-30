terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# CloudFront viewer certificates must be issued in us-east-1 regardless of
# the distribution's own region. Only the ACM cert resource in
# cloudfront.tf uses this; every other resource uses the default
# (eu-west-1) provider above and is unaffected.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

data "aws_caller_identity" "current" {}
