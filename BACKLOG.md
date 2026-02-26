# Bridge v2 — Беклог

## 🟡 Деплой AWS — почти готов

### Что сделано на AWS ✅
- IAM user `bridge-v2-deploy` (account: 882951811725)
- S3 Terraform state: `bridge-v2-tfstate-882951811725`
- Terraform apply — вся инфраструктура:
  - ECR: bridge-v2-{wa-service,processor,bot,analytics} — образы запушены
  - ECS кластер `bridge-v2-prod` — все 4 сервиса ACTIVE, running=1
  - RDS: `bridge-v2-db.c2pyaaesekjp.us-east-1.rds.amazonaws.com:5432`
  - ElastiCache Redis: `bridge-v2-redis.ppwzmo.0001.use1.cache.amazonaws.com`
  - EFS: `fs-0d16d94aeac63e4d1`
  - S3 media: `bridge-v2-media-882951811725`
  - Cloud Map: `bridge-v2-prod.local`

### Что осталось
1. **RDS schema migration** — применить `001_initial_schema.sql` на prod БД
   ```bash
   # Нужен туннель или временный bastion host
   # Или через ECS exec в один из контейнеров
   # credentials — см. Claude memory (backlog.md)
   aws ecs execute-command --cluster bridge-v2-prod \
     --task <task-id> --container processor \
     --command "/bin/bash" --interactive
   ```

2. **GitHub Actions secrets** — https://github.com/utkabotron/Bridge-v2/settings/secrets/actions
   - `AWS_ACCESS_KEY_ID` — см. Claude memory
   - `AWS_SECRET_ACCESS_KEY` — см. Claude memory
   - `AWS_ACCOUNT_ID` = 882951811725

3. **Проверить логи** — убедиться что сервисы стартовали без ошибок:
   ```bash
   aws logs tail /ecs/bridge-v2-prod/bot --follow --region us-east-1
   aws logs tail /ecs/bridge-v2-prod/processor --follow --region us-east-1
   ```

## terraform.tfvars (gitignored, воссоздать на новом компе)
Все значения хранятся в Claude memory (backlog.md). Шаблон: `infra/terraform/terraform.tfvars.example`

## Оставшиеся задачи из плана
- [ ] README.md с архитектурной диаграммой
- [ ] Prefect flows → Prefect Cloud
- [ ] BigQuery dataset
- [ ] End-to-end тест с реальным WhatsApp

## Сделано ✅
- [x] Все 4 сервиса + Docker Compose + тесты (13/13)
- [x] GitHub Actions (ci.yml + deploy.yml)
- [x] GitHub репо: https://github.com/utkabotron/Bridge-v2
- [x] Terraform + вся AWS инфраструктура (ECS running)
- [x] ECR push — все образы задеплоены
- [x] CLAUDE.md
- [x] Миграция v1→v2 (12 пользователей, 21 пара)
