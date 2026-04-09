# InterviewAI Pro — Python/Flask

Full conversion of the React + TypeScript application to a Python Flask backend
with Jinja2 templates. All AWS calls are server-side (security improvement).

## Bug Fixes from the Original React App

| # | Location | Bug | Fix |
|---|----------|-----|-----|
| 1 | `.env.example` | Listed both `GEMINI_API_KEY` and `OPENAI_API_KEY` but the code only used one | Now uses `OPENAI_API_KEY` consistently |
| 2 | `dynamodbService.ts` | AWS credentials embedded in browser bundle (`dangerouslyAllowBrowser`) | All AWS calls are now server-side; browser uses pre-signed S3 URLs |
| 3 | `dynamodbService.ts` | `sessionId = 'COMPLETED_' + Math.floor(Math.random() * 1000)` — only 1000 possible IDs | Uses `uuid4` for guaranteed uniqueness |
| 4 | `dynamodbService.ts` | `SK.split('#')[1]` breaks if sessionId contains `#` | Changed to `split('#', 1)` (maxsplit=1) |
| 5 | `aiService.ts` | Prompt asked for `DEEP_TECHNICAL` category but TypeScript type expected `technical` | Category normalization map added in Python service |
| 6 | `RegistrationView.tsx` | Old commented-out code left inside `handleSubmit` (unterminated try/catch mix) | Cleaned up; single code path |
| 7 | Admin dashboard | No backend API — sessions only came from React's `localStorage` | `/api/admin/sessions` endpoint with DynamoDB scan |

## Quick Start

```bash
# 1. Clone / extract project
cd InterviewAI-Python

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and fill in your API keys (see section below)

# 5. Run
python app.py
# → http://localhost:5000
```

## Environment Variables

See `.env.example` for full documentation. Required keys:

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Question generation via gpt-4o-mini |
| `AWS_ACCESS_KEY_ID` | DynamoDB + S3 access |
| `AWS_SECRET_ACCESS_KEY` | DynamoDB + S3 access |
| `AWS_REGION` | AWS region (e.g. `us-east-1`) |
| `DYNAMODB_TABLE` | Table name for sessions |
| `AWS_S3_BUCKET` | Bucket name for video uploads |
| `FLASK_SECRET_KEY` | Cookie signing secret |

## AWS Setup

### DynamoDB Table

- **Billing**: On-Demand (pay per request)
- **Partition key**: `PK` (String)
- **Sort key**: `SK` (String)

### IAM Policy for the app user

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:Scan"],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT:table/TABLE_NAME"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET/*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:GeneratePresignedUrl",
      "Resource": "*"
    }
  ]
}
```

### S3 CORS Configuration

Add this to your bucket's CORS settings to allow browser uploads:

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["PUT", "GET"],
    "AllowedOrigins": ["http://localhost:5000", "https://yourdomain.com"],
    "ExposeHeaders": []
  }
]
```

## Project Structure

```
InterviewAI-Python/
├── app.py                    # Flask routes
├── requirements.txt
├── .env.example
├── services/
│   ├── ai_service.py         # OpenAI question generation
│   └── dynamodb_service.py   # DynamoDB + S3 helpers
└── templates/
    ├── base.html             # Nav, footer, Tailwind CDN
    ├── home.html
    ├── register.html         # Create session form
    ├── start.html            # OTP entry + consent
    ├── interview.html        # Recording interface
    ├── report.html           # Results page
    └── admin.html            # Admin dashboard
```
