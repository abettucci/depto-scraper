output "lambda_function_url" {
  value       = aws_lambda_function_url.preview.function_url
  description = "URL privada por secreto compartido que Vercel usa para la vista previa."
}

output "runtime_secret_arn" {
  value       = aws_secretsmanager_secret.runtime.arn
  description = "ARN del secreto runtime que completa GitHub Actions."
}

output "cloudwatch_log_group" {
  value       = aws_cloudwatch_log_group.monitor.name
  description = "Grupo de logs para depurar ejecuciones y portales."
}
