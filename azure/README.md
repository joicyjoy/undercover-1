# Azure Container Apps Deployment (Option B)

Deploys Shoppie as three containers on **Azure Container Apps** backed by **Azure Database for PostgreSQL - Flexible Server**:

1. `shoppie-backend` (`azure/Dockerfile.backend`, `--only-group server`, external HTTPS ingress on port `8000`, serves `dashboard/dist` at `/`)
2. `shoppie-agent` (`azure/Dockerfile.agent`, `--only-group runtime`, internal-only ingress on port `8080`, verifies `Authorization: Bearer <UNDERCOVER_API_KEY>` except `health`)
3. `shoppie-context` (`azure/Dockerfile.context`, `--only-group context`, internal-only ingress on port `8006`, serves `/mcp` and `GET /health`)

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (`az`) logged in (`az login`)
- OpenRouter API key and a random `UNDERCOVER_API_KEY` of at least 32 characters

## 1. Provision ACR, PostgreSQL, and Container Apps

First create a resource group, provision the shared infrastructure (`deployApps=false` so the ACR and PostgreSQL server exist before pulling images), build the three Linux images in ACR, and then deploy all three Container Apps:

**macOS / Linux (zsh/bash):**

> Note: Some Azure subscriptions restrict new PostgreSQL Flexible Server deployments in `eastus` / `westus2`. Use a supported region such as `centralus`, `centralindia`, or `southeastasia` (or pass `postgresLocation=centralus` if your resource group is in `eastus`).

```sh
RG="shoppie-rg"
LOC="centralus"
az group create -n "$RG" -l "$LOC"

az deployment group create -g "$RG" -f azure/main.bicep \
  -p deployApps=false \
     postgresLocation="centralus" \
     postgresAdminPassword="<STRONG_PG_PASSWORD>" \
     openrouterApiKey="$OPENROUTER_API_KEY" \
     undercoverApiKey="$UNDERCOVER_API_KEY" \
     modelName=""

ACR=$(az deployment group show -g "$RG" -n main --query properties.outputs.acrName.value -o tsv)
ACR_LOGIN=$(az deployment group show -g "$RG" -n main --query properties.outputs.acrLoginServer.value -o tsv)

# Push images built locally for linux/amd64 (works even when ACR Tasks is restricted):
az acr login -n "$ACR"
docker build --platform linux/amd64 -f azure/Dockerfile.context -t "$ACR_LOGIN/shoppie-context:latest" .
docker build --platform linux/amd64 -f azure/Dockerfile.backend -t "$ACR_LOGIN/shoppie-backend:latest" .
docker build --platform linux/amd64 -f azure/Dockerfile.agent   -t "$ACR_LOGIN/shoppie-agent:latest" .
docker push "$ACR_LOGIN/shoppie-context:latest"
docker push "$ACR_LOGIN/shoppie-backend:latest"
docker push "$ACR_LOGIN/shoppie-agent:latest"

# Or, if ACR Tasks (`az acr build`) is permitted on your subscription:
# az acr build -r "$ACR" -f azure/Dockerfile.backend -t shoppie-backend:latest .
# az acr build -r "$ACR" -f azure/Dockerfile.agent -t shoppie-agent:latest .
# az acr build -r "$ACR" -f azure/Dockerfile.context -t shoppie-context:latest .

az deployment group create -g "$RG" -f azure/main.bicep \
  -p deployApps=true \
     postgresLocation="centralus" \
     postgresAdminPassword="<STRONG_PG_PASSWORD>" \
     openrouterApiKey="$OPENROUTER_API_KEY" \
     undercoverApiKey="$UNDERCOVER_API_KEY" \
     modelName=""
```

**Windows (PowerShell):**

```powershell
$RG = "shoppie-rg"
$LOC = "centralus"
az group create -n $RG -l $LOC

az deployment group create -g $RG -f azure/main.bicep `
  -p deployApps=false `
     postgresLocation="centralus" `
     postgresAdminPassword="<STRONG_PG_PASSWORD>" `
     openrouterApiKey="$env:OPENROUTER_API_KEY" `
     undercoverApiKey="$env:UNDERCOVER_API_KEY" `
     modelName=""

$ACR = az deployment group show -g $RG -n main --query properties.outputs.acrName.value -o tsv
$ACR_LOGIN = az deployment group show -g $RG -n main --query properties.outputs.acrLoginServer.value -o tsv

az acr login -n $ACR
docker build --platform linux/amd64 -f azure/Dockerfile.context -t "$ACR_LOGIN/shoppie-context:latest" .
docker build --platform linux/amd64 -f azure/Dockerfile.backend -t "$ACR_LOGIN/shoppie-backend:latest" .
docker build --platform linux/amd64 -f azure/Dockerfile.agent   -t "$ACR_LOGIN/shoppie-agent:latest" .
docker push "$ACR_LOGIN/shoppie-context:latest"
docker push "$ACR_LOGIN/shoppie-backend:latest"
docker push "$ACR_LOGIN/shoppie-agent:latest"

az deployment group create -g $RG -f azure/main.bicep `
  -p deployApps=true `
     postgresLocation="centralus" `
     postgresAdminPassword="<STRONG_PG_PASSWORD>" `
     openrouterApiKey="$env:OPENROUTER_API_KEY" `
     undercoverApiKey="$env:UNDERCOVER_API_KEY" `
     modelName=""
```

## 2. Database Seeding and Poolers

- On first startup, `shoppie-backend` runs `Store.initialize()`, creating the `undercover` schema, tables, `work_gate`, and initial 9 requests automatically.
- To connect via Azure Postgres PgBouncer transaction pooling instead of port `5432`, update the connection string port to `6432`: both [`backend/store.py`](../backend/store.py) (`port == 6432`) and [`agent/main.py`](../agent/main.py) disable prepared statements (`prepare_threshold=None`) automatically.

## 3. Security Recommendation

The dashboard has no application-level sign-in (`docs/SPEC.md`). Before sharing the public `backendUrl`, enable **Microsoft Entra ID authentication** (Easy Auth) or restrict inbound IPs on `shoppie-backend` in Azure Container Apps.
