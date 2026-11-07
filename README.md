# SmartBiz Backend (Flask API for Vercel + Neon)
JSON API for the SmartBiz Sales & Inventory system. Frontend lives in a separate repo.

## Database
Run `sql/neon_split/01_schema.sql` ... `05_demo_orders.sql` (05 is optional demo data) in the Neon SQL Editor. Use the **pooled** connection string (host has `-pooler`).

## Environment variables
| Name | Value |
|---|---|
| DATABASE_URL | Neon pooled connection string |
| SECRET_KEY | long random string (required on Vercel) |
| FRONTEND_ORIGIN | frontend URL after deploy, e.g. https://smartbiz-web.vercel.app (default `*`) |

## Run locally
    pip install -r requirements.txt
    export DATABASE_URL="..." SECRET_KEY="..."      # Windows: set NAME=value
    flask --app api/index run

## Deploy: GitHub -> Vercel
    git init && git add . && git commit -m "SmartBiz backend"
    git branch -M main
    git remote add origin https://github.com/<you>/smartbiz-backend.git
    git push -u origin main
Vercel -> Add New Project -> import repo -> Framework "Other" -> add the env vars -> Deploy.

## Test the deployed API
    curl https://<your-app>.vercel.app/api/health          # {"database":true,...}
    curl -X POST https://<your-app>.vercel.app/api/login -H "Content-Type: application/json" \
         -d '{"username":"admin","password":"<demo password>"}'      # returns a token
    curl https://<your-app>.vercel.app/api/dashboard/admin -H "Authorization: Bearer <token>"

## Endpoints
POST /api/login | POST /api/logout | GET /api/me | GET /api/health
GET /api/dashboard/<role>  (admin, inventory_manager, warehouse_manager, sales_manager, purchase_manager, sales_staff, customer, supplier)
POST /api/so/new, /api/so/{confirm|dispatch|cancel} | POST /api/po/quick, /api/po/{send|cancel|receive}
All except login/health need `Authorization: Bearer <token>`; role rules are enforced server-side.
