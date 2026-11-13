
from reporting.database import supabase

try:
    response = (
        supabase
        .table("branches")
        .select("branch_id, branch_name")
        .limit(5)
        .execute()
    )

    print("Connection successful!")
    print(response.data)

except Exception as error:
    print("Connection failed!")
    print(error)
