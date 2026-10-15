# Employee-Attendance-Analytics-API

Employee Attendance & Analytics API

A FastAPI-based employee attendance management system built with MongoDB. It supports employee management, attendance punch-in/punch-out, and attendance analytics.

1. Prerequisites

Install the following software before running the project:

- Python 3.13
- MongoDB Community Server
- Git (optional if downloading the project as a ZIP file)

2. Download the Project

Clone the repository:

git clone YOUR_GITHUB_REPOSITORY_URL
cd attandance-api

Alternatively, download the repository as a ZIP file from GitHub, extract it, and open the extracted project folder in a terminal or VS Code.

Replace "YOUR_GITHUB_REPOSITORY_URL" with the actual repository URL.

3. Create a Virtual Environment

On Windows, run:

py -3.13 -m venv .venv

Activate the environment:

.\.venv\Scripts\Activate.ps1

If using Command Prompt instead of PowerShell:

.venv\Scripts\activate.bat

4. Install Dependencies

With the virtual environment activated, run:

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

5. Configure Environment Variables

Create a file named ".env" in the project root directory, in the same folder as "requirements.txt".

Add the following configuration:

MONGO_URI=mongodb://127.0.0.1:27017
MONGO_DB=attendance_db

The application reads these values from environment variables to connect to MongoDB.

Important:

- Do not upload or commit ".env" to GitHub.
- The ".env" file must be created locally before starting the application.
- Ensure that the filename is ".env", not ".env.txt".

6. Start MongoDB

Install MongoDB Community Server and make sure its service is running.

The default local MongoDB connection URI is:

mongodb://127.0.0.1:27017

The application uses the database specified by "MONGO_DB". MongoDB must be running before using database-dependent endpoints.

7. Run the API

From the project root directory, run:

python -m uvicorn app.main:app --port 8000

For development with automatic reloading, run:

python -m uvicorn app.main:app --reload --port 8000

The application should start on port "8000".

8. Open the API Documentation

Once the server is running, open the following URL in a browser:

Swagger UI:
http://127.0.0.1:8000/docs

Health Check:
http://127.0.0.1:8000/health

Use Swagger UI to inspect and test the available endpoints, including employee management, attendance punch-in/punch-out, and analytics.

9. Testing

1. Ensure MongoDB is running.
2. Confirm that the ".env" file exists in the project root.
3. Start the API using the command above.
4. Open Swagger UI.
5. Test the available endpoints with appropriate request data.

10. Troubleshooting

MongoDB connection error

- Ensure MongoDB Community Server is installed and running.
- Verify "MONGO_URI" and "MONGO_DB" in ".env".
- Confirm that the configured MongoDB URI is reachable.

Missing environment variable error

If the application reports that "MONGO_URI" or "MONGO_DB" is missing:

- Check that ".env" exists in the project root.
- Check that both variables are spelled correctly.
- Ensure the file is named ".env", not ".env.txt".

Missing Python dependency

Activate the virtual environment and run:

python -m pip install -r requirements.txt

Port already in use

If port "8000" is already occupied, stop the other process using it or choose another port. If you change the port, open the matching URL in your browser.

11. Project Notes

- MongoDB must be running for database-dependent endpoints.
- Required MongoDB indexes are created by the application during startup.
- MongoDB connection settings are read from environment variables.
- The ".env" file and virtual environment should not be committed to GitHub.
