# Bridge v2 — Беклог

## ❌ AWS инфраструктура — удалена (2026-02-27)

Вся AWS инфраструктура снесена через `terraform destroy` + ручная очистка ECR/SG/S3.
Причина: дорого (~$60-100/мес), переход на VPS.

Удалено:
- ECS кластер `bridge-v2-prod` (4 сервиса)
- RDS PostgreSQL `bridge-v2-db`
- ElastiCache Redis `bridge-v2-redis`
- EFS `fs-0d16d94aeac63e4d1`
- ECR (4 репозитория с образами)
- S3: `bridge-v2-media-882951811725`, `bridge-v2-tfstate-882951811725`, `bridge-storage-pk`
- Security groups, Cloud Map, CloudWatch Logs
- IAM user `bridge-v2-deploy` (account: 882951811725) — ключи в CSV, можно деактивировать

## Следующий шаг: деплой на VPS

TODO:
- [ ] Выбрать VPS провайдера
- [ ] Настроить docker compose на VPS
- [ ] Применить schema migration
- [ ] Настроить CI/CD для VPS (GitHub Actions → SSH deploy)
- [ ] End-to-end тест с реальным WhatsApp

## Оставшиеся задачи из плана
- [x] README.md с архитектурной диаграммой
- [ ] Prefect flows → Prefect Cloud
- [ ] BigQuery dataset
- [ ] End-to-end тест с реальным WhatsApp

## Сделано ✅
- [x] Все 4 сервиса + Docker Compose + тесты (13/13)
- [x] GitHub Actions (ci.yml + deploy.yml)
- [x] GitHub репо: https://github.com/utkabotron/Bridge-v2
- [x] Terraform + AWS инфраструктура (удалена 2026-02-27)
- [x] ECR push (удалено)
- [x] CLAUDE.md
- [x] Миграция v1→v2 (12 пользователей, 21 пара)
