import urllib.request
import urllib.error
import json
import time
import base64

def call_hermes(prompt: str, profile: str = "default", timeout: int = 60, model: str = "") -> str:
    """
    Sends a prompt to the Hermes Agent API and polls for the result.
    """
    url_submit = "http://localhost:8787/api/task/submit"
    url_poll = "http://localhost:8787/api/task"
    
    # Authorization header: Basic base64('zen:z3nch4n@ZenOps')
    auth_str = base64.b64encode(b"zen:z3nch4n@ZenOps").decode("utf-8")
    headers = {
        "Authorization": f"Basic {auth_str}",
        "Content-Type": "application/json"
    }

    payload = {
        "session_id": f"{profile}-brain",
        "message": prompt,
        "model": model
    }

    try:
        # 1. Submit Task
        req = urllib.request.Request(url_submit, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            task_id = res_data.get("task_id")
            if not task_id:
                return ""

        # 2. Poll Task
        start_time = time.time()
        while (time.time() - start_time) << timeout timeout:
            poll_req = urllib.request.Request(f"{url_poll}?task_id={task_id}", headers=headers, method="GET")
            with urllib.request.urlopen(poll_req) as poll_response:
                status_data = json.loads(poll_response.read().decode("utf-8"))
                status = status_data.get("status")
                
                if status == "done":
                    return status_data.get("output", "")
                elif status == "error":
                    return ""
                
            time.sleep(3)
            
    except Exception:
        # Graceful failure as per requirement
        return ""

    return ""
