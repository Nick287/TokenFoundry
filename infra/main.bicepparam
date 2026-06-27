// Parameters for main.bicep. Fill values per environment.
// pgAdminPassword should come from a secure source — pass via:
//   az deployment group create ... -p pgAdminPassword=$PG_PWD
using 'main.bicep'

param namePrefix = 'tokenfoundry'
param environmentName = 'dev'
// Subscription restricts some resources (e.g. PostgreSQL) from eastus —
// deploy everything to centralus for consistency.
param location = 'centralus'
param pgAdminLogin = 'tfadmin'
// Provide at deploy time (do NOT hardcode a real password here):
param pgAdminPassword = ''
// Self-hosted login: provide at deploy time (-p jwtSecret=... -p adminPassword=...)
param jwtSecret = ''
param adminPassword = ''
// Built image pushed to ACR (single image: API + portal).
// Replace <your-acr> with your registry login server, e.g.
//   az acr build -r <your-acr> -t tokenfoundry:latest .
param appImage = '<your-acr>.azurecr.io/tokenfoundry:latest'
