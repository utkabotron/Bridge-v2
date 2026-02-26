variable "aws_region" {
  default = "us-east-1"
}

variable "project" {
  default = "bridge-v2"
}

variable "env" {
  default = "prod"
}

variable "telegram_bot_token" {
  sensitive = true
}

variable "openai_api_key" {
  sensitive = true
}

variable "langchain_api_key" {
  sensitive = true
}

variable "db_password" {
  sensitive = true
  default   = "changeme_in_tfvars"
}

variable "admin_tg_ids" {
  description = "Comma-separated Telegram user IDs for admins"
  default     = ""
}
