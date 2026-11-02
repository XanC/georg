#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Create or update the Georg CloudFormation stack. See docs/deploy-aws.md.
#   deploy/aws/deploy.sh VpcId=vpc-... SubnetId=subnet-... ImageId=ami-... [Name=Value ...]
set -eu
REGION=${REGION:-us-east-2}
STACK=${STACK:-georg}
exec aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$(dirname "$0")/georg.yaml" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides "$@"
