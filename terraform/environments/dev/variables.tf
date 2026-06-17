# environments/dev/variables.tf
variable "aws_region"         { type = string }
variable "vpc_id"             { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "ops_email"          { type = string; sensitive = true; default = "" }
variable "project"            { type = string; default = "annam" }
variable "owner_team"         { type = string; default = "ml-infrastructure" }
variable "image_tag"          { type = string; default = "latest" }
variable "cw_namespace"       { type = string; default = "AnnamAI/GapFilling" }
