# Referência de Configuração

Este projeto usa um arquivo `config.json` na raiz para controlar a migração.

Você pode partir deste exemplo:

```json
{
  "plone_url": "https://seu-plone.exemplo.gov.br/site/pt-br",
  "plone_token": "Bearer JWT_TOKEN_AQUI",
  "plone_news_folder": "/noticias",
  "source_base": "https://www.gov.br",
  "source_start": "https://www.gov.br/orgaos/exemplo/pt-br/assuntos/noticias",
  "delay": 1,
  "all_pages": true,
  "max_news": 0,
  "progress_file": "migracao_progresso.json",
  "portal_type": "Document",
  "migrate_as_self": true,
  "skip_files": false
}
```

## Variáveis

- `plone_url`: URL base do site Plone 6 de destino.
- `plone_token`: token JWT usado na autenticação da API. O script aceita com ou sem prefixo `Bearer `.
- `plone_news_folder`: caminho da pasta ou página de destino dentro do Plone.
- `source_base`: domínio base usado para resolver links relativos encontrados na origem.
- `source_start`: URL inicial da listagem ou da página única que será migrada.
- `delay`: intervalo, em segundos, entre requisições HTTP.
- `all_pages`: se `true`, percorre a paginação da origem; se `false`, processa só a URL inicial.
- `max_news`: limite de itens processados. Use `0` para processar tudo.
- `progress_file`: arquivo local usado para retomar migrações interrompidas.
- `portal_type`: tipo de conteúdo criado ou atualizado no Plone.
- `migrate_as_self`: se `true`, aplica o conteúdo diretamente na própria pasta/página de destino; se `false`, cria itens filhos dentro da pasta.
- `skip_files`: se `true`, não faz upload de anexos e tenta apenas religar os links para arquivos já existentes no Plone.

## Valores de `portal_type`

- `Document`: para páginas comuns, páginas institucionais e páginas com anexos.
- `News Item`: para notícias com blocos específicos como lead image, social share e text-to-speech.

Hoje o app e o script foram preparados para esses dois valores.

## Combinações Comuns

### 1. Migrar uma listagem de notícias

Use quando a origem tem várias notícias paginadas e você quer criar itens filhos no destino.

```json
{
  "plone_news_folder": "/noticias",
  "source_start": "https://www.gov.br/orgaos/exemplo/pt-br/assuntos/noticias",
  "all_pages": true,
  "portal_type": "News Item",
  "migrate_as_self": false,
  "skip_files": false
}
```

### 2. Migrar uma página institucional com anexos

Use quando a origem é uma página única e o conteúdo deve ser aplicado diretamente na página de destino.

```json
{
  "plone_news_folder": "/acesso-a-informacao/acordos-de-cooperacao-tecnica/2023",
  "source_start": "https://www.gov.br/orgaos/exemplo/pt-br/acesso-a-informacao/acordos-de-cooperacao-tecnica/2023",
  "all_pages": false,
  "portal_type": "Document",
  "migrate_as_self": true,
  "skip_files": false
}
```

### 3. Reprocessar conteúdo sem reenviar anexos

Use quando os arquivos já foram migrados antes e você quer apenas reconstruir os links no corpo.

```json
{
  "portal_type": "Document",
  "migrate_as_self": true,
  "skip_files": true
}
```

## Observações

- `progress_file` guarda as URLs já processadas.
- Se quiser reexecutar tudo do zero, apague o arquivo de progresso.
- Para repositórios públicos, não publique `config.json` com token real.
- Prefira manter no Git apenas `config.example.json`.
