# Remote state in S3 — create bucket manually before first terraform init:
#   aws s3 mb s3://bridge-v2-tfstate-<account_id> --region us-east-1
#   aws dynamodb create-table --table-name bridge-v2-tflock \
#     --attribute-definitions AttributeName=LockID,AttributeType=S \
#     --key-schema AttributeName=LockID,KeyType=HASH \
#     --billing-mode PAY_PER_REQUEST

terraform {
  backend "s3" {
    bucket         = "bridge-v2-tfstate"   # replace with your bucket name
    key            = "prod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "bridge-v2-tflock"
    encrypt        = true
  }
}
