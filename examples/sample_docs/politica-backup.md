# Política de backup da empresa

## Escopo

Todos os bancos de dados de produção e repositórios de código devem ter
backup diário. Arquivos de usuários em laptops não são cobertos por esta
política; use o drive corporativo.

## Frequência e retenção

Backups completos acontecem aos domingos às 02:00 UTC. Backups incrementais
acontecem diariamente às 02:00 UTC. A retenção é de 35 dias para incrementais
e 12 meses para completos mensais. Backups anuais são guardados por 5 anos
em armazenamento frio.

## Teste de restauração

Todo trimestre o time de plataforma deve restaurar um backup completo em
ambiente isolado e validar a integridade dos dados. O resultado é registrado
no relatório de conformidade. Um backup não testado não é um backup.

## Criptografia

Todos os backups são criptografados com AES-256 antes de sair do servidor.
As chaves ficam no cofre de segredos, com rotação anual. É proibido guardar
chaves junto aos arquivos de backup.
