# ARGUS — Plataforma de Monitoramento

## Visão geral

ARGUS é uma plataforma interna de monitoramento de infraestrutura. Ela coleta
métricas de servidores, serviços e filas, e dispara alertas quando limiares
são ultrapassados. O nome vem do gigante de cem olhos da mitologia grega.

## Arquitetura

O ARGUS é composto por quatro serviços principais:

- **Collector**: agente leve instalado em cada host, envia métricas a cada 15 segundos.
- **Ingestor**: recebe as métricas via gRPC e escreve no banco de séries temporais.
- **Alerter**: avalia regras de limiar e envia notificações para o Slack e PagerDuty.
- **Dashboard**: interface web com gráficos em tempo real e histórico de 90 dias.

## Retenção de dados

As métricas brutas são mantidas por 30 dias. Agregações de 5 minutos são
mantidas por 90 dias, e agregações horárias por 2 anos. O banco usado é o
TimescaleDB, uma extensão do PostgreSQL.

## Runbook de alerta de disco

Quando o alerta `disk_usage_high` disparar:

1. Verifique qual partição está cheia com `df -h`.
2. Logs antigos em `/var/log/argus` podem ser removidos com segurança após 7 dias.
3. Se o crescimento for do banco, avalie reduzir a retenção bruta para 15 dias.
4. Nunca apague o diretório `wal/` manualmente.

## Contatos

O time responsável é o SRE. Escalação primária: canal #argus-alertas.
