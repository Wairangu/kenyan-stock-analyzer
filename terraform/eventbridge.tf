resource "aws_cloudwatch_event_rule" "trigger" {
  name                = "${var.project_name}-market-open"
  description         = "Triggers the NSE dashboard/summary Lambda at market open (09:00 EAT), Mon-Fri."
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "analyzer" {
  rule = aws_cloudwatch_event_rule.trigger.name
  arn  = aws_lambda_function.analyzer.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.analyzer.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.trigger.arn
}
