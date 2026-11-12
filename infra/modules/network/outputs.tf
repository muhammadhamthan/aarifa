output "vpc_id" {
  value = aws_vpc.this.id
}

output "vpc_cidr" {
  value = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  value = [for s in aws_subnet.public : s.id]
}

output "private_subnet_ids" {
  value = [for s in aws_subnet.private : s.id]
}

output "data_subnet_ids" {
  value = [for s in aws_subnet.data : s.id]
}

output "app_security_group_id" {
  description = "Attach to every ECS task; RDS/Redis trust this group."
  value       = aws_security_group.app.id
}

output "nat_public_ips" {
  description = "Static egress IPs (give to Meta/Twilio allow-lists if ever needed)."
  value       = [for e in aws_eip.nat : e.public_ip]
}
