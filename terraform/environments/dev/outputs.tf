# environments/dev/outputs.tf

output "data_bucket_name"              { value = module.s3.bucket_id }
output "ecr_repository_url"            { value = module.ecr.repository_url }
output "state_machine_arn"             { value = module.stepfunctions.state_machine_arn }
output "failure_topic_arn"             { value = module.stepfunctions.failure_topic_arn }
output "ecs_cluster_arn"               { value = module.ecs.cluster_arn }
output "sensor_table_name"             { value = module.dynamodb.sensor_table_name }
output "results_table_name"            { value = module.dynamodb.results_table_name }
output "task_security_group_id"        { value = module.networking.task_security_group_id }
output "task_role_arn"                 { value = module.iam.task_role_arn }
output "run_batch_task_definition_arn" { value = module.ecs.run_batch_task_definition_arn }
