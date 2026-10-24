# Architecture review
## Summary
- Overall posture: The architecture establishes a modern, serverless-frontend and containerized-backend foundation using CloudFront, ALB, ECS Fargate across two AZs, RDS PostgreSQL, and S3, but requires critical hardening around network perimeter security and availability guardrails.
- Biggest risk: The Application Load Balancer and RDS PostgreSQL instance lack strict private subnet isolation and ingress boundaries, exposing internal components directly to potential external threats.
- Quickest win: Enable Amazon S3 public access blocks, secure transport, KMS encryption, and access logging on the user upload bucket, alongside applying CloudFront HTTP security headers.

## Top priorities
| # | Pillar | Finding | Recommendation | Source |
|---|---|---|---|---|
| 1 | Security | RDS and S3 and ALB exposure | Deploy RDS and ALB in private subnets with strict security groups limiting inbound traffic to ECS tasks, and enforce TLS/SSL for RDS connections. | [configure RDS VPC network security](https://docs.aws.amazon.com/fedramp/latest/userguide/amazon-rds.html) |
| 2 | Security | S3 bucket security | Block public access, require secure transport, enable server-side encryption with AWS KMS, and maintain access logging on user uploads bucket. | [S3 bucket security best practices](https://docs.aws.amazon.com/code-library/latest/ug/bash_2_s3_code_examples.html) |
| 3 | Reliability | ECS Fargate tasks capacity constraints in two AZs | Configure appropriate minimum task counts and Auto Scaling policies to maintain compute availability during AZ degradation. | [Amazon ECS capacity and availability best practices](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/capacity-availability.html) |
| 4 | Cost optimization | ECS Fargate tasks over-provisioning | Use AWS Compute Optimizer to analyze utilization metrics and adjust task CPU and memory configurations to match workload requirements. | [Fargate task right sizing](https://docs.aws.amazon.com/prescriptive-guidance/latest/optimize-costs-microsoft-workloads/optimizer-ecs-fargate.html) |
| 5 | Security | CloudFront security headers | Implement HSTS, X-Content-Type-Options, and Content-Security-Policy headers using CloudFront Functions or viewer response headers. | [CloudFront HTTP security headers](https://docs.aws.amazon.com/solutions/latest/distributed-load-testing-on-aws/security-1.html) |

## Cost optimization
- **[Medium]** <ECS Fargate tasks over-provisioning> — Use AWS Compute Optimizer to analyze historical CPU and memory utilization metrics of your ECS services running on Fargate, and adjust task CPU and memory configurations in your task definitions to match actual workload requirements ([Fargate task right sizing](https://docs.aws.amazon.com/prescriptive-guidance/latest/optimize-costs-microsoft-workloads/optimizer-ecs-fargate.html))
- **[Low]** <RDS database sizing and resource selection> — Right-size your Amazon RDS PostgreSQL instance by evaluating workload demand and selecting the appropriate resource type and size using cost modeling ([resource rightsizing](https://docs.aws.amazon.com/cost-management/latest/userguide/cost-optimization-hub.html))

## Security
- **[High]** <RDS and S3 and ALB exposure> — Ensure the Amazon RDS PostgreSQL instance and Application Load Balancer are deployed in private subnets with strict security groups limiting inbound traffic only from ECS Fargate tasks, and enforce TLS/SSL for all connections to RDS ([configure RDS VPC network security](https://docs.aws.amazon.com/fedramp/latest/userguide/amazon-rds.html))
- **[Medium]** <S3 bucket security> — Secure the S3 bucket used for user uploads by blocking public access, requiring secure transport (SSL), enabling server-side encryption with AWS KMS, and maintaining access logging ([S3 bucket security best practices](https://docs.aws.amazon.com/code-library/latest/ug/bash_2_s3_code_examples.html))
- **[Medium]** <CloudFront security headers> — Implement security headers (such as HSTS, X-Content-Type-Options, and Content-Security-Policy) on responses from CloudFront using CloudFront Functions or viewer response headers ([CloudFront HTTP security headers](https://docs.aws.amazon.com/solutions/latest/distributed-load-testing-on-aws/security-1.html))
- **[Low]** <RDS least privilege and master user security> — Apply least-privilege IAM permissions for database management and manage master credentials securely using AWS Secrets Manager rather than embedding credentials in plaintext ([least privilege permissions for RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_BestPractices.Security.html))

## Reliability
- **[Medium]** <ECS Fargate tasks spanning two Availability Zones can experience capacity constraints during an Availability Zone impairment; configure appropriate minimum task counts and Auto Scaling policies> — <Ensure that ECS capacity and availability best practices are implemented to maintain sufficient compute resources during AZ degradation> ([Amazon ECS capacity and availability best practices](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/capacity-availability.html))
- **[Low]** <CloudFront sits in front of the ALB without an explicit secondary origin group configured for origin failover> — <Implement origin failover and origin groups in CloudFront to automatically redirect traffic to a secondary origin if the primary ALB origin becomes unavailable> ([CloudFront origin failover](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/high_availability_origin_failover.html))

## Trade-offs
- **Multi-AZ Availability vs. Cost:** Deploying resources (such as ECS Fargate tasks and RDS PostgreSQL) across multiple Availability Zones increases fault tolerance and resilience against infrastructure failures, but incurs higher data transfer and operational costs compared to a single-AZ configuration.
- **Security Isolation vs. Operational Complexity:** Restricting the ALB and RDS PostgreSQL instance strictly to private subnets with fine-grained security groups improves defense-in-depth, but can introduce additional operational overhead for troubleshooting and connectivity management.

## Sources
- [Fargate task right sizing](https://docs.aws.amazon.com/prescriptive-guidance/latest/optimize-costs-microsoft-workloads/optimizer-ecs-fargate.html)
- [resource rightsizing](https://docs.aws.amazon.com/cost-management/latest/userguide/cost-optimization-hub.html)
- [configure RDS VPC network security](https://docs.aws.amazon.com/fedramp/latest/userguide/amazon-rds.html)
- [S3 bucket security best practices](https://docs.aws.amazon.com/code-library/latest/ug/bash_2_s3_code_examples.html)
- [CloudFront HTTP security headers](https://docs.aws.amazon.com/solutions/latest/distributed-load-testing-on-aws/security-1.html)
- [least privilege permissions for RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_BestPractices.Security.html)
- [Amazon ECS capacity and availability best practices](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/capacity-availability.html)
- [CloudFront origin failover](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/high_availability_origin_failover.html)

---

Input: `A public web app: CloudFront in front of an ALB, ECS Fargate tasks in two AZs, an RDS PostgreSQL instance, and an S3 bucket for user uploads.`

```
  → [reliability_reviewer] search_documentation(search_intent='Multi-AZ failover and scaling for Amazon RDS PostgreSQL and ECS Fargate'...)
  → [cost_reviewer] search_documentation(search_phrase='ECS Fargate cost optimization right sizing', search_intent='ECS Fargate ...)
  → [security_reviewer] search_documentation(search_phrase='CloudFront ALB ECS RDS S3 security best practices', search_intent='Searc...)
  → [reliability_reviewer] search_documentation(search_intent='CloudFront high availability and origin failover reliability', search_ph...)
  → [cost_reviewer] search_documentation(search_phrase='RDS PostgreSQL cost optimization right sizing', search_intent='RDS Postg...)
  → [security_reviewer] search_documentation(search_intent='Search for Amazon RDS security best practices encryption IAM', search_ph...)
— 14.5s · 10 model calls · 6/15 tool calls {'search_documentation': 6} · 91,764 in / 2,778 out tokens
✓ All sections present and every AWS doc link was returned by a tool.
```
