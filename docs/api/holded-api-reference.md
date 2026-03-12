# Holded API — Complete Reference

> Source: https://developers.holded.com/reference
> Fetched: 2026-03-12
> Purpose: Local reference for holded-connector development

---

## General

- **Base URL (Invoicing):** `https://api.holded.com/api/invoicing/v1`
- **Base URL (Projects):** `https://api.holded.com/api/projects/v1`
- **Base URL (Accounting):** `https://api.holded.com/api/accounting/v1`
- **Base URL (Team):** `https://api.holded.com/api/team/v1`
- **Base URL (CRM):** `https://api.holded.com/api/crm/v1`
- **Auth:** API Key in header (`key: YOUR_API_KEY`)
- **Format:** JSON request/response bodies
- **Dates:** Unix timestamps (integers)
- **Pagination:** All GET list endpoints are paginated — `?page=2` for next page

---

## 1. DOCUMENTS

Manage invoices, estimates, purchases, credit notes, and other document types.

### Document Types (`docType` values)

| docType | Description |
|---------|-------------|
| `invoice` | Sales invoice |
| `salesreceipt` | Sales receipt |
| `creditnote` | Credit note |
| `receiptnote` | Receipt note |
| `estimate` | Estimate / presupuesto |
| `salesorder` | Sales order |
| `waybill` | Waybill / albarán |
| `proform` | Proforma invoice |
| `purchase` | Purchase invoice |
| `purchaserefund` | Purchase refund |
| `purchaseorder` | Purchase order |

### List Documents

```
GET /documents/{docType}
```

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `starttmp` | string | Starting Unix timestamp filter |
| `endtmp` | string | Ending Unix timestamp filter |
| `contactid` | string | Filter by contact ID |
| `paid` | string | Payment status: 0=unpaid, 1=paid, 2=partially paid |
| `billed` | string | Billing status: 0=unbilled, 1=billed |
| `sort` | string | `created-asc` or `created-desc` |
| `page` | integer | Pagination (default page 1) |

**Response:** Array of document objects

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique document identifier |
| `contact` | string | Contact ID |
| `contactName` | string | Contact name |
| `desc` | string | Document description |
| `date` | integer | Document date (Unix timestamp) |
| `dueDate` | integer | Due date (Unix timestamp) |
| `notes` | string | Internal notes |
| `docNumber` | string | Document number (e.g., F170001) |
| `products` | array | Line items (see below) |
| `tax` | number | Total tax amount |
| `subtotal` | number | Subtotal before tax/discount |
| `discount` | integer | Discount percentage |
| `total` | number | Final total |
| `language` | string | Language code |
| `status` | integer | Document status (see status codes) |
| `currency` | string | Currency code (e.g., `eur`) |
| `currencyChange` | integer | Exchange rate multiplier |
| `paymentsTotal` | integer | Total payments received |
| `paymentsPending` | number | Outstanding amount |
| `paymentsRefunds` | integer | Refund total |
| `salesChannelId` | string | Sales channel ID |
| `customFields` | array | Custom field key-value pairs |
| `tags` | array | Document tags |

**Line Item Fields (products array):**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Product/service name |
| `desc` | string | Line item description |
| `price` | number | Unit price |
| `units` | number | Quantity |
| `tax` | integer | Tax percentage |
| `discount` | integer | Line discount percentage |
| `retention` | integer | Retention amount |
| `weight` | integer | Weight |
| `costPrice` | number | Cost price |
| `sku` | string | Product SKU |
| `productId` | string | Product identifier |
| `projectid` | string | Project ID (**lowercase**, not camelCase) |
| `kind` | string | Item kind |

### Get Document

```
GET /documents/{docType}/{documentId}
```

Returns full document detail (same fields as list + expanded nested objects).

### Create Document

```
POST /documents/{docType}
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `contactId` | string | No* | Existing contact ID |
| `contactCode` | string | No* | Contact NIF/CIF/VAT |
| `contactName` | string | No* | Name (creates new contact if needed) |
| `contactEmail` | string | No | Contact email |
| `contactAddress` | string | No | Billing address |
| `contactCity` | string | No | City |
| `contactCp` | string | No | Postal code |
| `contactProvince` | string | No | Province |
| `contactCountryCode` | string | No | Country code |
| `date` | integer | **Yes** | Document date (Unix timestamp) |
| `dueDate` | integer | No | Payment due date |
| `desc` | string | No | Description |
| `notes` | string | No | Notes |
| `approveDoc` | boolean | No | Auto-approve (default: false) |
| `applyContactDefaults` | boolean | No | Apply contact defaults (default: true) |
| `salesChannelId` | string | No | Sales channel ID |
| `paymentMethodId` | string | No | Payment method ID |
| `currency` | string | No | ISO currency (eur, usd, etc.) |
| `currencyChange` | number | No | Exchange rate |
| `designId` | string | No | Document template ID |
| `language` | string | No | Language (es, en, fr, de, it, ca, eu) |
| `numSerieId` | string | No | Numbering series ID |
| `invoiceNum` | string | No | Custom invoice number |
| `tags` | array | No | Document tags |
| `customFields` | array | No | Custom fields (`[{field, value}]`) |
| `directDebitProvider` | string | No | e.g., "gocardless" |

*At least one contact identifier is needed (contactId, contactCode, or contactName).

**Items array:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Item name |
| `desc` | string | Item description |
| `units` | number | Quantity |
| `subtotal` | number | Line subtotal before tax |
| `discount` | number | Discount |
| `tax` | integer | VAT percentage |
| `taxes` | array | Comma-separated tax keys (e.g., `"s_iva_21,s_ret_19"`) |
| `sku` | string | SKU |
| `serviceId` | string | Service ID |
| `accountingAccountId` | string | GL account |
| `supplied` | string | "Yes" or "No" |
| `tags` | array | Item tags |

**Shipping fields:** `warehouseId`, `shippingAddress`, `shippingPostalCode`, `shippingCity`, `shippingProvince`, `shippingCountry`

**Response (201):**
```json
{
  "status": 1,
  "id": "document_id",
  "invoiceNum": "F170007",
  "contactId": "contact_id"
}
```

### Update Document

```
PUT /documents/{docType}/{documentId}
```

**Request Body (all optional):**
`desc`, `notes`, `language`, `date`, `paymentMethod`, `warehouseId`, `items` (array), `salesChannelId`, `expAccountId`, `customFields`

Items support: `name`, `desc`, `subtotal`, `tax`, `units`, `discount`, `tags`, `accountingAccountId`, `kind`, `sku`, `lotSku` (when kind=lots), `supplied`

**Response:**
```json
{ "status": 1, "info": "Updated", "id": "documentId" }
```

### Delete Document

```
DELETE /documents/{docType}/{documentId}
```

### Pay Document

```
POST /documents/{docType}/{documentId}/pay
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `date` | integer | Yes | Payment date (Unix timestamp) |
| `amount` | number | Yes | Payment amount |
| `treasury` | string | No | Treasury account Holded ID |
| `desc` | string | No | Payment description |

**Response:**
```json
{
  "status": 1,
  "invoiceId": "doc_id",
  "invoiceNum": "F170007",
  "paymentId": "payment_id"
}
```

### Send Document (Email)

```
POST /documents/{docType}/{documentId}/send
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `emails` | string | Yes | Comma-separated emails |
| `mailTemplateId` | string | No | Email template ID |
| `subject` | string | No | Subject (min 10 chars) |
| `message` | string | No | Body (min 20 chars) |
| `docIds` | string | No | Additional document IDs |

### Get Document PDF

```
GET /documents/{docType}/{documentId}/pdf
```

**Response:**
```json
{
  "status": 1,
  "info": "...",
  "data": "<base64-encoded PDF>"
}
```

### Document Shipping Operations

```
POST /documents/{docType}/{documentId}/shipping
```

### Update Document Pipeline

```
PUT /documents/{docType}/{documentId}/pipeline
```

### Document File Attachments

```
POST /documents/{docType}/{documentId}/attach
GET  /documents/{docType}/{documentId}/attachments
```

### List Payment Methods

```
GET /documents/paymentmethods
```

---

## 2. CONTACTS

### List Contacts

```
GET /contacts
```

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `phone` | string | Exact match (including special chars) |
| `mobile` | string | Exact match (including special chars) |
| `customId` | array | Filter by custom IDs |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Contact ID |
| `customId` | string | Custom reference ID |
| `name` | string | Contact name |
| `code` | string | NIF/CIF/VAT code |
| `tradeName` | string | Trade name |
| `email` | string | Email |
| `mobile` | string | Mobile phone |
| `phone` | string | Phone |
| `type` | string | `supplier`, `debtor`, `creditor`, `client`, `lead` |
| `iban` | string | IBAN |
| `swift` | string | SWIFT code |
| `clientRecord` | integer | Client record number |
| `supplierRecord` | integer | Supplier record number |
| `billAddress` | object | `{address, city, postalCode, province, country}` |
| `defaults` | object | Sales channel, taxes, payment method defaults |
| `socialNetworks` | object | Website and social links |
| `tags` | array | Tags |
| `notes` | array | Notes with timestamps |
| `contactPersons` | array | Contact persons |
| `shippingAddresses` | array | Shipping addresses |
| `customFields` | array | Custom field key-value pairs |

### Create Contact

```
POST /contacts
```

**Request Body:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Contact name |
| `code` | string | NIF/CIF/VAT |
| `tradeName` | string | Trade name |
| `email` | string | Email |
| `mobile` | string | Mobile |
| `phone` | string | Phone |
| `type` | string | `supplier`, `debtor`, `creditor`, `client`, `lead` |
| `isperson` | boolean | True = Contact Person (not Company) |
| `iban` | string | IBAN |
| `swift` | string | SWIFT |
| `sepaRef` | string | SEPA reference |
| `sepaDate` | number | SEPA date |
| `groupId` | string | Contact group ID |
| `taxOperation` | string | `general`, `intra`, `impexp`, `nosujeto`, `receq`, `exento` |
| `billAddress` | object | `{address, city, postalCode, province, country}` |
| `defaults` | object | Sales/purchase defaults, payment terms, taxes, currency, language |
| `socialNetworks` | object | Website and social links |
| `tags` | array | Tags |
| `note` | string | Notes |
| `contactPersons` | array | `[{name, phone, email}]` |
| `shippingAddresses` | array | Shipping addresses |
| `numberingSeries` | object | Custom numbering for documents |
| `CustomId` | string | Custom reference ID |

### Get Contact

```
GET /contacts/{contactId}
```

### Update Contact

```
PUT /contacts/{contactId}
```

### Delete Contact

```
DELETE /contacts/{contactId}
```

### Contact Attachments

```
GET /contacts/{contactId}/attachments/list      — List filenames
GET /contacts/{contactId}/attachments/get?filename=X  — Get file
```

---

## 3. PRODUCTS

### List Products

```
GET /products
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Product ID |
| `kind` | string | Product kind (`simple`, `pack`, etc.) |
| `name` | string | Product name |
| `desc` | string | Description |
| `typeId` | string | Type ID |
| `contactId` | string | Supplier contact ID |
| `contactName` | string | Supplier name |
| `price` | number | Sale price |
| `tax` | integer | Tax percentage |
| `total` | number | Price with tax |
| `rates` | array | Price rates |
| `hasStock` | integer | Stock tracking enabled |
| `stock` | integer | Current stock |
| `barcode` | string | Barcode |
| `sku` | string | SKU |
| `cost` | number | Cost |
| `purchasePrice` | number | Purchase price |
| `weight` | number | Weight |
| `tags` | array | Tags |
| `categoryId` | string | Category ID |
| `factoryCode` | string | Factory/manufacturer code |
| `attributes` | array | Product attributes |
| `forSale` | integer | Available for sale |
| `forPurchase` | integer | Available for purchase |
| `salesChannelId` | string | Sales channel ID |
| `expAccountId` | string | Expense account ID |
| `warehouseId` | string | Default warehouse ID |
| `variants` | array | `[{id, barcode, sku, price, cost, purchasePrice, stock}]` |

### Create Product

```
POST /products
```

**Request Body:** `kind`, `name`, `desc`, `price`, `tax`, `cost`, `calculatecost`, `purchasePrice`, `tags`, `barcode`, `sku`, `weight`, `stock`

**Response:** `{status, info, id}`

### Get Product

```
GET /products/{productId}
```

### Update Product

```
PUT /products/{productId}
```

**Request Body:** `kind`, `name`, `desc`, `tax`, `subtotal`, `barcode`, `sku`, `cost`, `purchasePrice`, `weight`

### Delete Product

```
DELETE /products/{productId}
```

### Product Images

```
GET /products/{productId}/image          — Main image
GET /products/{productId}/imagesList     — All images
GET /products/{productId}/image/{imageFileName}  — Specific image
```

### Update Stock

```
PUT /products/{productId}/stock
```

**Request Body:**
```json
{
  "stock": {
    "warehouseId": "warehouse_id",
    "quantity": 10
  }
}
```

---

## 4. PAYMENTS

### List Payments

```
GET /payments
```

**Query Parameters:** `starttmp`, `endtmp` (Unix timestamps)

**Response Fields:** `id`, `bankId`, `contactId`, `contactName`, `amount`, `desc`, `date`

### Create Payment

```
POST /payments
```

**Request Body:** `bankId`, `contactId`, `amount` (number), `desc`, `date` (integer)

### Get Payment

```
GET /payments/{paymentId}
```

### Update Payment

```
PUT /payments/{paymentId}
```

### Delete Payment

```
DELETE /payments/{paymentId}
```

---

## 5. TREASURIES

### List Treasury Accounts

```
GET /treasury
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Treasury ID |
| `name` | string | Account name |
| `type` | string | Account type (e.g., "bank") |
| `balance` | integer | Current balance |
| `accountNumber` | integer | Bank account number |
| `iban` | string | IBAN |
| `swift` | string | SWIFT code |
| `bank` | string | Bank identifier |
| `bankname` | string | Bank display name |

### Create Treasury Account

```
POST /treasury
```

**Request Body:** `name`, `type`, `balance`, `accountNumber`, `iban`, `swift`, `bank`, `bankname`

### Get Treasury Account

```
GET /treasury/{treasuryId}
```

---

## 6. TAXES

### Get Taxes

```
GET /taxes
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Tax name (e.g., "IVA 21%") |
| `amount` | string | Tax rate |
| `scope` | string | `sales` or `purchases` |
| `key` | string | Unique key (e.g., `s_iva_21`) |
| `group` | string | Tax group (e.g., `iva`) |
| `type` | string | e.g., `percentage` |

---

## 7. SERVICES

### List Services

```
GET /services
```

### Create Service

```
POST /services
```

**Request Body:** `name`, `desc`, `tags` (array), `tax` (number), `subtotal` (integer), `salesChannelId`, `cost` (number)

### Get / Update / Delete Service

```
GET    /services/{serviceId}
PUT    /services/{serviceId}
DELETE /services/{serviceId}
```

---

## 8. NUMBERING SERIES

```
GET    /numberseries/{docType}        — Get by document type
POST   /numberseries                  — Create
PUT    /numberseries/{numberSerieId}  — Update
DELETE /numberseries/{numberSerieId}  — Delete
```

---

## 9. EXPENSE ACCOUNTS

```
GET    /expaccounts           — List
POST   /expaccounts           — Create
GET    /expaccounts/{id}      — Get
PUT    /expaccounts/{id}      — Update
DELETE /expaccounts/{id}      — Delete
```

---

## 10. SALES CHANNELS

```
GET    /saleschannels           — List
POST   /saleschannels           — Create
GET    /saleschannels/{id}      — Get
PUT    /saleschannels/{id}      — Update
DELETE /saleschannels/{id}      — Delete
```

---

## 11. WAREHOUSES

```
GET    /warehouses              — List
POST   /warehouses              — Create
GET    /warehouses/{id}         — Get
PUT    /warehouses/{id}         — Update
DELETE /warehouses/{id}         — Delete
GET    /warehouses/{id}/stock   — Get warehouse stock
```

---

## 12. CONTACT GROUPS

```
GET    /contactgroups           — List
POST   /contactgroups           — Create
GET    /contactgroups/{id}      — Get
PUT    /contactgroups/{id}      — Update
DELETE /contactgroups/{id}      — Delete
```

---

## 13. REMITTANCES

```
GET /remittances               — List
GET /remittances/{id}          — Get
```

---

## 14. PROJECTS API

Base URL: `https://api.holded.com/api/projects/v1`

### Projects

```
GET    /projects                       — List all
POST   /projects                       — Create (body: {name})
GET    /projects/{projectId}           — Get
PUT    /projects/{projectId}           — Update
DELETE /projects/{projectId}           — Delete
GET    /projects/{projectId}/summary   — Project summary (evolution, profitability)
```

**Project Response Fields:** `id`, `name`, `desc`, `tags`, `category`, `contactId`, `contactName`, `date`, `dueDate`, `status`, `lists`, `billable`, `expenses`, `estimates`, `sales`, `timeTracking`, `price`, `numberOfTasks`, `completedTasks`, `labels`

### Tasks

```
GET    /tasks              — List all tasks
POST   /tasks              — Create (body: {projectId, listId, name})
GET    /tasks/{taskId}     — Get
DELETE /tasks/{taskId}     — Delete
```

**Task Fields:** `id`, `projectId`, `listId`, `name`, `desc`, `labels`, `comments`, `dates`, `userId`, `status`, `billable`, `featured`

### Time Tracking

```
GET    /projects/{projectId}/times                     — List project times
POST   /projects/{projectId}/times                     — Create (body: {duration, costHour, desc, userId, taskId})
GET    /projects/{projectId}/times/{timeTrackingId}     — Get
PUT    /projects/{projectId}/times/{timeTrackingId}     — Update
DELETE /projects/{projectId}/times/{timeTrackingId}     — Delete
GET    /projects/times                                  — List ALL times (params: start, end, archived)
```

**Time Fields:** `timeId`, `duration`, `desc`, `costHour`, `userId`, `taskId`, `total`

---

## 15. ACCOUNTING API

Base URL: `https://api.holded.com/api/accounting/v1`

### Daily Ledger

```
GET  /dailyledger    — List entries (params: page, starttmp, endtmp; max 500/page)
POST /entry          — Create entry
```

**Create Entry Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `date` | integer | Yes | Entry date (Unix timestamp) |
| `notes` | string | No | Entry notes |
| `lines` | array | Yes | Entry lines (see below) |

**Line fields:** `account` (integer, required), `credit` (number), `debit` (number), `description` (string), `tags` (array)

### Chart of Accounts

```
GET  /chartofaccounts  — List (params: starttmp, endtmp, includeEmpty=0|1)
POST /account          — Create (body: {prefix (4-digit), name, color})
```

---

## 16. TEAM API

Base URL: `https://api.holded.com/api/team/v1`

### Employees

```
GET    /employees                — List all (params: page; 500/page max)
POST   /employees                — Create (body: {name, lastName, email, sendInvite?})
GET    /employees/{employeeId}   — Get
PUT    /employees/{employeeId}   — Update
DELETE /employees/{employeeId}   — Delete
```

**Update fields:** `name`, `lastName`, `mainEmail`, `email`, `nationality`, `phone`, `mobile`, `dateOfBirth` (dd/mm/yyyy), `gender`, `mainLanguage`, `IBAN`, `timeOffPolicyId`, `timeOffSupervisors`, `reportingTo`, `code` (NIF), `socialSecurityNum`, `address`, `fiscalAddress`, `workplace`, `teams`, `holdedUserId`

### Employee Time Tracking

```
GET    /employees/times                          — All time trackings
GET    /employees/times/{employeeTimeId}          — Get specific
PUT    /employees/times/{employeeTimeId}          — Update (body: {startTmp, endTmp})
DELETE /employees/times/{employeeTimeId}          — Delete
GET    /employees/{employeeId}/times              — Employee-specific times
POST   /employees/{employeeId}/times              — Create (body: {startTmp, endTmp})
POST   /employees/{employeeId}/times/clockin      — Clock in (optional: location)
POST   /employees/{employeeId}/times/clockout     — Clock out (optional: lat, long)
POST   /employees/{employeeId}/times/pause        — Pause
POST   /employees/{employeeId}/times/unpause      — Unpause
```

---

## 17. CRM API

Base URL: `https://api.holded.com/api/crm/v1`

### Funnels

```
GET    /funnels              — List
POST   /funnels              — Create
GET    /funnels/{funnelId}   — Get
PUT    /funnels/{funnelId}   — Update
DELETE /funnels/{funnelId}   — Delete
```

### Leads

```
GET    /leads              — List all
POST   /leads              — Create
GET    /leads/{leadId}     — Get
PUT    /leads/{leadId}     — Update
DELETE /leads/{leadId}     — Delete
```

**Lead sub-resources:**

```
POST   /leads/{leadId}/notes    — Add note
PUT    /leads/{leadId}/notes    — Update note
POST   /leads/{leadId}/tasks    — Add task
PUT    /leads/{leadId}/tasks    — Update task
DELETE /leads/{leadId}/tasks    — Delete task
PUT    /leads/{leadId}/dates    — Update creation date
PUT    /leads/{leadId}/stages   — Update funnel stage
```

### Events

```
GET    /events              — List
POST   /events              — Create
GET    /events/{eventId}    — Get
PUT    /events/{eventId}    — Update
DELETE /events/{eventId}    — Delete
```

### Bookings

```
GET  /bookings/locations                  — List locations
GET  /bookings/{locationId}/available     — Available slots
GET  /bookings                            — List bookings
POST /bookings                            — Create
GET  /bookings/{bookingId}                — Get
PUT  /bookings/{bookingId}                — Update
DELETE /bookings/{bookingId}              — Delete
```

---

## Document Status Codes

| Status | Invoice / Purchase | Estimate |
|--------|-------------------|----------|
| 0 | Draft | Draft |
| 1 | Issued | Pending |
| 2 | Partially paid | Accepted |
| 3 | Paid | Rejected |
| 4 | Overdue | Invoiced |
| 5 | Cancelled | — |

---

## Known Quirks & Gotchas

1. **`projectid` not `projectId`** — Line items use lowercase `projectid`
2. **`desc` is a PG reserved word** — Always quote as `"desc"` in SQL
3. **Empty strings fail in NUMERIC** — Holded API returns `""` for some numeric fields; sanitize with `_num()`
4. **Purchases page 2 timeout** — Holded API times out on page 2 of purchases; page 1 has all records
5. **Tags are arrays** — Store as `json.dumps(item.get('tags') or [])`
6. **Pagination** — All list endpoints paginated; use `?page=N`
7. **Dates** — All dates are Unix timestamps (integers), not ISO strings
8. **Rate limits** — Not documented; implement backoff
9. **`customFields`** — Array of `{field: "field_id", value: "value"}` objects
