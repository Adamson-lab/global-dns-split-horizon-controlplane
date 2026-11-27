terraform {
  required_version = ">= 1.5.0"
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "dns_state_bucket" {
  type    = string
  default = "global-dns-failover-state"
}

output "aws_region" {
  value = var.aws_region
}

output "dns_state_bucket" {
  value = var.dns_state_bucket
}
