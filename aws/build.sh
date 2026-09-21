#!/bin/bash
# Build the Lambda container image and push it to ECR.
#
# Usage: ./aws/build.sh [tag]
#   tag defaults to the current git short SHA.
#
# Requires: docker, aws CLI configured, and the ECR repository already
# created by Terraform (terraform apply -target=aws_ecr_repository.lambda,
# or just run terraform apply for everything first).
set -euo pipefail
cd "$(dirname "$0")/.."

AWS_REGION="${AWS_REGION:-eu-west-1}"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPO_NAME="kenyan-stock-analyzer-lambda"
REPO_URL="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO_NAME}"
TAG="${1:-$(git rev-parse --short HEAD)}"

echo "Building ${REPO_URL}:${TAG} ..."
docker build -t "${REPO_URL}:${TAG}" -t "${REPO_URL}:latest" -f aws/Dockerfile .

echo "Logging in to ECR ..."
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "Pushing ${REPO_URL}:${TAG} and :latest ..."
docker push "${REPO_URL}:${TAG}"
docker push "${REPO_URL}:latest"

echo "Done. Image: ${REPO_URL}:${TAG}"
echo "If the Lambda function already exists, update it with:"
echo "  aws lambda update-function-code --function-name kenyan-stock-analyzer --image-uri ${REPO_URL}:${TAG} --region ${AWS_REGION}"
