from fastapi import FastAPI, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse,StreamingResponse,JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from typing import Optional
from bson import ObjectId
import motor.motor_asyncio
import hashlib
import csv
from fastapi.responses import StreamingResponse
from io import StringIO
from datetime import datetime,date , timedelta
from utils import generate_reset_token, verify_reset_token,send_reset_email,send_email
from passlib.hash import bcrypt
from collections import defaultdict





# Initialize FastAPI app
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="YOUR_SECRET_KEY_HERE")

# MongoDB setup
MONGO_DETAILS = "mongodb://localhost:27017"
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_DETAILS)
db = client.tracker
expense_collection = db.expense
user_collection = db.user
budget_collection = db.budget

# Jinja2 template setup
templates = Jinja2Templates(directory="templates")

# Utility functions
def hash_password(password: str):
    return hashlib.sha256(password.encode()).hexdigest()

def get_current_user(request: Request) -> Optional[str]:
    return request.session.get("user")

def is_bcrypt_hash(pw: str):
    return pw.startswith("$2a$") or pw.startswith("$2b$") or pw.startswith("$2y$")



# Pydantic models
class Expense(BaseModel):
    amount: float
    category: str
    description: str
    payment_method: str
    date: str
    user: str

class User(BaseModel):
    name: str
    password: str
    
def get_current_user(request: Request) -> Optional[str]:
    return request.session.get("user")

# Routes
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse("intro.html", {"request": request})

@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_form(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})

@app.post("/forgot-password")
async def send_reset(request: Request, email: str = Form(...)):
    user = await user_collection.find_one({"email": email})
    if user:
        token = generate_reset_token(email)
        send_reset_email(email, token)
    return RedirectResponse("/login", status_code=302)

@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_form(request: Request, token: str):
    email = verify_reset_token(token)
    if not email:
        return HTMLResponse("Invalid or expired token", status_code=400)
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})

@app.post("/reset-password")
async def reset_password(token: str = Form(...), password: str = Form(...)):
    email = verify_reset_token(token)
    if not email:
        return HTMLResponse("Invalid or expired token", status_code=400)
    hashed_pw = bcrypt.hash(password)
    await user_collection.update_one({"email": email}, {"$set": {"password": hashed_pw}})
    return RedirectResponse("/login", status_code=302)

@app.get("/export", response_class=HTMLResponse)
async def export_page(request: Request):
    username = get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=302)

    expenses = await expense_collection.find({"user": username}).to_list(length=1000)
    budget = await budget_collection.find({"user": username}).to_list(length=1000)

    # Optional sorting by category
    budget.sort(key=lambda b: b.get("category", ""))

    return templates.TemplateResponse("export.html", {
        "request": request,
        "expenses": expenses,
        "budget": budget
    })
    
@app.get("/view-expense", response_class=HTMLResponse)
async def view_expense(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    cursor = expense_collection.find({"user": user})
    expenses = await cursor.to_list(length=1000)

    for item in expenses:
        item["date"] = item["date"].strftime("%Y-%m-%d")

    # Group by month and category
    monthly_summary_dict = defaultdict(float)
    for e in expenses:
        month = datetime.strptime(e["date"], "%Y-%m-%d").strftime("%Y-%m")
        key = (month, e["category"])
        monthly_summary_dict[key] += e["amount"]

    # Convert to list of dicts for Jinja
    monthly_summary = [
        {"month": k[0], "category": k[1], "amount": round(v, 2)}
        for k, v in monthly_summary_dict.items()
    ]

    return templates.TemplateResponse("view_expenses.html", {
        "request": request,
        "expenses": expenses,
        "monthly_summary": monthly_summary
    })

@app.get("/export-csv")
async def export_csv(request: Request):
    username = get_current_user(request)
    expenses = await expense_collection.find({"user": username}).to_list(length=1000)

    csv_file = StringIO()
    writer = csv.writer(csv_file)
    writer.writerow(["Category", "Amount", "Date"])

    for item in expenses:
        writer.writerow([
            item.get("category", ""),
            item.get("amount", ""),
            item.get("date", "")
        ])

    csv_file.seek(0)
    return StreamingResponse(
        csv_file,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=expenses.csv"}
    )

@app.get("/export-budget-csv")
async def export_budget_csv(request: Request):
    username = get_current_user(request)
    budget_data = await budget_collection.find({"user": username}).to_list(length=1000)

    csv_file = StringIO()
    writer = csv.writer(csv_file)
    writer.writerow(["Category", "Planned Budget (₹)"])

    for item in budget_data:
        writer.writerow([
            item.get("category", ""),
            item.get("amount", "")
        ])

    csv_file.seek(0)
    return StreamingResponse(
        csv_file,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=monthly_budget.csv"}
    )


@app.get("/budgetform", response_class=HTMLResponse)
async def budget_form(request: Request, month: int = None, year: int = None):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", status_code=302)

    # If no month/year provided, use current
    now = datetime.now()
    selected_month = month if month else now.month
    selected_year = year if year else now.year

    # Aggregate expenses for the selected month/year
    pipeline = [
    {
        "$addFields": {
            "month": { "$month": { "$toDate": "$date" } },
            "year": { "$year": { "$toDate": "$date" } }
        }
    },
    {
        "$match": {
            "user": user,
            "month": selected_month,
            "year": selected_year
        }
    },
    {
        "$group": {
            "_id": { "$toLower": "$category" },
            "total": { "$sum": "$amount" }
        }
    }
]


    totals = {"food_spent": 0, "travel_spent": 0, "movies_spent": 0}
    cursor = expense_collection.aggregate(pipeline)
    async for doc in cursor:
        category = doc["_id"]
        total = doc["total"]
        if category == "food":
            totals["food_spent"] = total
        elif category == "travel":
            totals["travel_spent"] = total
        elif category == "movies":
            totals["movies_spent"] = total

    # Fetch the matching budget document for that user/month/year
    budget_data = await budget_collection.find_one({
        "user": user,
        "month": selected_month,
        "year": selected_year
    })

    if not budget_data:
        budget_data = {"food": 0, "movie": 0, "travel": 0}

    food_budget = budget_data.get("food", 0)
    travel_budget = budget_data.get("travel", 0)
    movie_budget = budget_data.get("movie", 0)
    total_budget = food_budget + travel_budget + movie_budget
    food_spent = totals["food_spent"]
    travel_spent = totals["travel_spent"]
    movies_spent = totals["movies_spent"]
    total_spent = food_spent + travel_spent + movies_spent

    return templates.TemplateResponse("budgetform.html", {
        "request": request,
        "food_budget": food_budget,
        "travel_budget": travel_budget,
        "movie_budget": movie_budget,
        "total_budget": total_budget,
        "food_spent": food_spent,
        "travel_spent": travel_spent,
        "movies_spent": movies_spent,
        "total_spent": total_spent,
        "selected_month": selected_month,
        "selected_year": selected_year
    })

@app.get("/index", response_class=HTMLResponse)
async def home(request: Request):
    if "user" not in request.session:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request, "user": request.session["user"]})


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    user = await user_collection.find_one({"name": username})
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"})

    stored_pw = user["password"]

    if is_bcrypt_hash(stored_pw):
        valid = bcrypt.verify(password, stored_pw)
        print(valid)
    else:
        valid = stored_pw == password
    print(stored_pw)
    print(valid)

    if not valid:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"})

    # Upgrade old plain text password to bcrypt
    if not is_bcrypt_hash(stored_pw):
        new_hash = bcrypt.hash(password)
        await user_collection.update_one({"name": username}, {"$set": {"password": new_hash}})

    # Store user in session
    request.session["user"] = username

    # Send login alert email
    if "email" in user:
        login_time = datetime.now().strftime("%d %B %Y at %I:%M %p")
        subject = "🛡️ Login Alert: Expense Tracker"
        body = f"Hi {user['name']},\n\nYou successfully logged in to your Expense Tracker on {login_time}.\n\nRegards,\nExpense Tracker Team"
        send_email(subject, body, user["email"])

    return RedirectResponse("/dashboard", status_code=302)


@app.post("/submit-budget", response_class=HTMLResponse)
async def submit_budget(
    request: Request,
    food: float = Form(...),
    movie: float = Form(...),
    travel: float = Form(...),
    budget_month: int = Form(...),
    budget_year: int = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Get today's day number (1-31)
    today = date.today()
    budget_day = today.day

    # Validate input
    if food < 0 or movie < 0 or travel < 0:
        return templates.TemplateResponse("budgetform.html", {
            "request": request,
            "error": "Values must be positive numbers."
        })

    total_budget = food + travel + movie

    # Budget data to store
    budget_data = {
        "user": user,
        "food": food,
        "movie": movie,
        "travel": travel,
        "total_budget": total_budget,
        "day": budget_day,
        "month": budget_month,
        "year": budget_year,
    }

    # Check if budget for user on this date exists
    existing = await budget_collection.find_one({
        "user": user,
        "day": budget_day,
        "month": budget_month,
        "year": budget_year
    })

    if existing:
        await budget_collection.update_one(
            {"_id": existing["_id"]},
            {"$set": budget_data}
        )
    else:
        await budget_collection.insert_one(budget_data)

    return RedirectResponse("/dashboard", status_code=302)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

@app.get("/register", response_class=HTMLResponse)
async def register_get(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})

@app.post("/register", response_class=HTMLResponse)
async def register_post(request: Request, name: str = Form(...),email: str = Form(...), password: str = Form(...)):
    existing_user = await user_collection.find_one({"name": name})
    if existing_user:
        return templates.TemplateResponse("register.html", {"request": request, "error": "User already exists"})
    hashed_password = bcrypt.hash(password)
    await user_collection.insert_one({"name": name, "password": hashed_password,"email":email})
    return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    username = get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

    user = await user_collection.find_one({"name": username})
    if not user:
        return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

    try:
        selected_month = int(request.query_params.get("filter_month", datetime.now().month))
        selected_year = int(request.query_params.get("filter_year", datetime.now().year))
    except ValueError:
        selected_month = datetime.now().month
        selected_year = datetime.now().year

    filter_category = request.query_params.get("filter_category", "")
    filter_date = request.query_params.get("filter_date", "")
    filter_type = request.query_params.get("filter_type", "month")

    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    # Budget for the selected month and year
    budget_doc = await budget_collection.find_one({
        "user": username,
        "month": selected_month,
        "year": selected_year
    })
    budget = budget_doc.get("total_budget", 0) if budget_doc else 0

    # Get expenses for the selected month & year
    cursor = expense_collection.find({
        "user": username,
        "$expr": {
            "$and": [
                {"$eq": [{"$month": {"$dateFromString": {"dateString": "$date"}}}, selected_month]},
                {"$eq": [{"$year": {"$dateFromString": {"dateString": "$date"}}}, selected_year]}
            ]
        }
    })
    expenses = await cursor.to_list(length=1000)

    total_spent = sum(e.get("amount", 0) for e in expenses)
    remaining = budget - total_spent

    # Fetch all monthly expenses
    all_expenses = expenses  # already fetched above

    # Category Totals (used for pie chart)
    category_totals = {}
    for e in all_expenses:
        cat = e.get("category", "Others")
        category_totals[cat] = category_totals.get(cat, 0) + e.get("amount", 0)

    chart_labels = []
    chart_data = []
    monthly_totals = {}
    yearly_category_monthly_totals = {month: {} for month in month_names}

    # --- Filter Logic ---
    if filter_type == "date" and filter_date:
        filtered = [e for e in expenses if e.get("date") == filter_date]
        if filter_category:
            filtered = [e for e in filtered if e.get("category") == filter_category]
        chart_labels = [e.get("category", "") for e in filtered]
        chart_data = [e.get("amount", 0) for e in filtered]

    elif filter_type == "month":
        if filter_category:
            filtered = [e for e in expenses if e.get("category") == filter_category]
            date_totals = {}
            for e in filtered:
                date = e.get("date")
                date_totals[date] = date_totals.get(date, 0) + e.get("amount", 0)
            chart_labels = list(date_totals.keys())
            chart_data = list(date_totals.values())

        # Category-wise totals for current month
        for e in expenses:
            cat = e.get("category", "Others")
            monthly_totals[cat] = monthly_totals.get(cat, 0) + e.get("amount", 0)

    elif filter_type == "year":
        # Get all expenses for selected year
        year_expenses = await expense_collection.find({
            "user": username,
            "$expr": {
                "$eq": [{"$year": {"$dateFromString": {"dateString": "$date"}}}, selected_year]
            }
        }).to_list(length=1000)

        if filter_category:
            month_totals = {m: 0 for m in range(1, 13)}
            for e in year_expenses:
                if e.get("category") == filter_category:
                    m = datetime.strptime(e["date"], "%Y-%m-%d").month
                    month_totals[m] += e.get("amount", 0)

            chart_labels = [month_names[m - 1] for m in range(1, 13)]
            chart_data = [month_totals[m] for m in range(1, 13)]

            # For pie chart
            category_totals = {
                filter_category: sum(e["amount"] for e in year_expenses if e["category"] == filter_category)
            }

            # For grouped bar
            for e in year_expenses:
                if e["category"] == filter_category:
                    m = datetime.strptime(e["date"], "%Y-%m-%d").month
                    month_name = month_names[m - 1]
                    yearly_category_monthly_totals[month_name][filter_category] = yearly_category_monthly_totals[month_name].get(filter_category, 0) + e["amount"]

            monthly_totals = {month_names[m - 1]: amt for m, amt in month_totals.items()}

        else:
            # Fill monthly_totals = 0 for all 12 months first
            monthly_totals = {month: 0 for month in month_names}

            pipeline = [
                {"$match": {
                    "user": username,
                    "$expr": {
                        "$eq": [{"$year": {"$dateFromString": {"dateString": "$date"}}}, selected_year]
                    }
                }},
                {"$group": {
                    "_id": {
                        "month": {"$month": {"$dateFromString": {"dateString": "$date"}}},
                        "category": "$category"
                    },
                    "total": {"$sum": "$amount"}
                }}
            ]

            results = await expense_collection.aggregate(pipeline).to_list(length=None)
            for item in results:
                month = month_names[item["_id"]["month"] - 1]
                category = item["_id"]["category"]
                yearly_category_monthly_totals[month][category] = yearly_category_monthly_totals[month].get(category, 0) + item["total"]

            # Now calculate overall monthly totals
            pipeline_month = [
                {"$match": {
                    "user": username,
                    "$expr": {
                        "$eq": [{"$year": {"$dateFromString": {"dateString": "$date"}}}, selected_year]
                    }
                }},
                {"$group": {
                    "_id": {"$month": {"$dateFromString": {"dateString": "$date"}}},
                    "total": {"$sum": "$amount"}
                }}
            ]

            results_month = await expense_collection.aggregate(pipeline_month).to_list(length=None)

            for item in results_month:
                month_index = item["_id"] - 1
                if 0 <= month_index < 12:
                    month_name = month_names[month_index]
                    monthly_totals[month_name] = item["total"]

    # --- Date category totals ---
    date_filtered_expenses = [e for e in expenses if e.get("date") == filter_date]
    date_total = sum(e.get("amount", 0) for e in date_filtered_expenses)

    date_category_totals = {}
    for e in date_filtered_expenses:
        cat = e.get("category", "Others")
        date_category_totals[cat] = date_category_totals.get(cat, 0) + e.get("amount", 0)

    # Yearly category total (pie chart when filter_type=year and no category)
    yearly_category_totals = {}
    if filter_type == "year" and not filter_category:
        pipeline = [
            {"$match": {
                "user": username,
                "$expr": {
                    "$eq": [{"$year": {"$dateFromString": {"dateString": "$date"}}}, selected_year]
                }
            }},
            {"$group": {
                "_id": "$category",
                "total": {"$sum": "$amount"}
            }}
        ]
        results = await expense_collection.aggregate(pipeline).to_list(length=None)
        for r in results:
            yearly_category_totals[r["_id"]] = r["total"]

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": username,
        "budget": budget,
        "remaining": remaining,
        "expenses": expenses,
        "total_spent": total_spent,
        "category_totals": category_totals,
        "filter_category": filter_category,
        "filter_date": filter_date,
        "filter_type": filter_type,
        "chart_labels": chart_labels,
        "chart_data": chart_data,
        "date_total": date_total,
        "date_category_totals": date_category_totals,
        "selected_month": selected_month,
        "selected_year": selected_year,
        "monthly_totals": monthly_totals,
        "yearly_category_totals": yearly_category_totals,
        "yearly_category_monthly_totals": yearly_category_monthly_totals
    })



@app.get("/fields", response_class=HTMLResponse)
async def add_expense_form(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("editfields.html", {"request": request, "expense": None})

@app.get("/viewexpense", response_class=HTMLResponse)
async def view_expense(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

    cursor = expense_collection.find({"user": user})
    expenses = await cursor.to_list(length=1000)
    return templates.TemplateResponse("viewexpense.html", {"request": request, "expenses": expenses})


@app.post("/addexpense", response_class=HTMLResponse)
async def add_expense_post(
    request: Request,
    amount: float = Form(...),
    category: str = Form(...),
    description: str = Form(...),
    payment_method: str = Form(...),
    date: str = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

    # MongoDB budget_entry fetch
    budget_entry = await budget_collection.find_one({"user": user})

    if budget_entry:
        # Category names -> budget & spent key mapping
        category_lower = category.lower()
        budget_key = category_lower
        spent_key = f"{category_lower}_spent"

        # Fetch values from DB
        category_budget = budget_entry.get(budget_key, float('inf'))
        category_spent = budget_entry.get(spent_key, 0)

        # Add new amount to spent & compare with budget
        new_total_spent = category_spent + amount
        if new_total_spent > category_budget:
            return templates.TemplateResponse("fields.html", {
                "request": request,
                "error": f"This expense ₹{amount} exceeds your {category} budget of ₹{category_budget}!",
                "amount": amount,
                "category": category,
                "description": description,
                "payment_method": payment_method,
                "date": date,
            })

    # Save expense if within budget
    expense_data = {
        "amount": amount,
        "category": category,
        "description": description,
        "payment_method": payment_method,
        "date": date,
        "user": user,
    }
    await expense_collection.insert_one(expense_data)

    return templates.TemplateResponse("fields.html", {
        "request": request,
        "message": "Expense Added Successfully!"
    })


@app.get("/editexpense/{expense_id}", response_class=HTMLResponse)
async def edit_expense_get(request: Request, expense_id: str):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    expense = await expense_collection.find_one({"_id": ObjectId(expense_id), "user": user})
    if not expense:
        return RedirectResponse("/viewexpense")

    expense["_id"] = str(expense["_id"])

    return templates.TemplateResponse("editfields.html", {
        "request": request,
        "expense": expense,
        "user": user
    })

@app.post("/editexpense/{expense_id}", response_class=HTMLResponse)
async def edit_expense_post(
    request: Request,
    expense_id: str,
    amount: float = Form(...),
    category: str = Form(...),
    description: str = Form(...),
    payment_method: str = Form(...),
    date: str = Form(...)
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    existing = await expense_collection.find_one({"_id": ObjectId(expense_id), "user": user})
    if not existing:
        return RedirectResponse("/viewexpense")

    update_data = {
        "amount": amount,
        "category": category,
        "description": description,
        "payment_method": payment_method,
        "date": date,
        "user": user
    }

    await expense_collection.update_one({"_id": ObjectId(expense_id)}, {"$set": update_data})

    return RedirectResponse("/viewexpense", status_code=302)

@app.post("/deleteexpense/{expense_id}")
async def delete_expense(expense_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    existing = await expense_collection.find_one({"_id": ObjectId(expense_id), "user": user})
    if not existing:
        return RedirectResponse("/viewexpense")

    await expense_collection.delete_one({"_id": ObjectId(expense_id)})
    return RedirectResponse("/viewexpense", status_code=302)

@app.post("/set-budget")
async def set_budget(request: Request, budget: int = Form(...)):
    if "user" not in request.session:
        return RedirectResponse("/login", status_code=302)
    
    username = request.session["user"]

    await user_collection.update_one(
        {"name": username},
        {"$set": {"budget": budget}}
    )
    
    return RedirectResponse("/dashboard", status_code=302)

@app.get("/export-monthly-expense")
async def export_monthly_expense(request: Request, month: str):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Extract selected month and year
    try:
        target_month = datetime.strptime(month, "%Y-%m")
    except ValueError:
        return RedirectResponse("/view-expenses", status_code=302)

    selected_month_str = target_month.strftime("%Y-%m")

    # Fetch all user expenses (because filtering in DB won't work on strings)
    cursor = expense_collection.find({"user": user})
    expenses = await cursor.to_list(length=1000)

    # Filter manually for selected month
    summary = defaultdict(float)
    for e in expenses:
        try:
            if e["date"].startswith(selected_month_str):
                category = e.get("category", "Uncategorized")
                summary[category] += e["amount"]
        except:
            continue

    # Generate CSV
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Month", "Category", "Total Amount"])

    for category, amount in summary.items():
        writer.writerow([month, category, round(amount, 2)])

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={month}_overview.csv"}
    )
    
    
@app.get("/monthly-summary")
async def monthly_summary(request: Request, month: str):
    user = get_current_user(request)
    if not user:
        return []

    try:
        target_month = datetime.strptime(month, "%Y-%m")
    except ValueError:
        return []

    selected_month_str = target_month.strftime("%Y-%m")

    cursor = expense_collection.find({"user": user})
    expenses = await cursor.to_list(length=1000)

    summary = defaultdict(float)
    for e in expenses:
        try:
            if e["date"].startswith(selected_month_str):
                summary[e["category"]] += e["amount"]
        except:
            continue

    result = [{"category": k, "amount": round(v, 2)} for k, v in summary.items()]
    return JSONResponse(result)

@app.post("/send-monthly-csv")
async def send_monthly_csv(request: Request, month: str = Form(...)):
    username = get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

    # Fetch user document
    user_doc = await user_collection.find_one({"name": username})
    if not user_doc or "email" not in user_doc:
        return JSONResponse({"error": "Email not found for user"}, status_code=400)

    # Parse month like "2025-06"
    try:
        year, month_num = map(int, month.split("-"))
    except ValueError:
        return JSONResponse({"error": "Invalid month format"}, status_code=400)

    # Get expenses for the selected month/year
    cursor = expense_collection.find({
        "user": username,
        "$expr": {
            "$and": [
                {"$eq": [{"$month": {"$dateFromString": {"dateString": "$date"}}}, month_num]},
                {"$eq": [{"$year": {"$dateFromString": {"dateString": "$date"}}}, year]}
            ]
        }
    })
    expenses = await cursor.to_list(length=None)

    # Build CSV content
    csv_content = "Category,Amount,Date\n"
    for exp in expenses:
        csv_content += f"{exp.get('category','')},{exp.get('amount',0)},{exp.get('date','')}\n"

    # Send email with attachment
    send_email(
        subject=f"Your Monthly Expense Report - {month}",
        body="Hi, please find attached your monthly expense CSV report.",
        to_email=user_doc["email"],
        attachment=csv_content,
        filename=f"{username}_{month}_expenses.csv"
    )

    return JSONResponse({"message": "Expense report emailed successfully!"})

