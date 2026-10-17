variable "aws_region" {
  type        = string
  description = "Región donde se ejecuta la Lambda."
  default     = "us-east-2"
}

variable "lambda_package_path" {
  type        = string
  description = "Ruta local al ZIP generado por GitHub Actions."
  default     = "../build/depto-scraper-lambda.zip"
}

variable "lambda_s3_key" {
  type        = string
  description = "Ubicación versionada del ZIP dentro del bucket de artefactos."
  default     = "releases/depto-scraper-lambda.zip"
}
