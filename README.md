# aaa
多 Agent 自动化系统
import os
import re
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from openai import OpenAI


# =========================
# 1. 基础配置
# =========================

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()

if not OPENAI_API_KEY:
    raise RuntimeError("请先在 .env 中配置 OPENAI_API_KEY")

WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI(
    title="Multi-Agent Dev Automation System",
    description="一个多 Agent 自动化研发系统：需求分析、代码生成、测试、审查、文档总结。",
    version="1.0.0",
)


# =========================
# 2. 请求与响应模型
# =========================

class DevTaskRequest(BaseModel):
    task: str = Field(..., description="用户提出的研发任务，例如：为项目增加登录接口")
    max_rounds: int = Field(default=2, ge=1, le=5, description="最多自动修复轮数")
    run_tests: bool = Field(default=True, description="是否自动执行测试")


class AgentMessage(BaseModel):
    agent: str
    content: Any


class DevTaskResponse(BaseModel):
    task: str
    success: bool
    rounds: int
    messages: List[AgentMessage]
    changed_files: List[str]
    test_output: Optional[str] = None
    review_result: Optional[Dict[str, Any]] = None
    summary: Optional[str] = None


# =========================
# 3. 文件系统安全工具
# =========================

def safe_path(relative_path: str) -> Path:
    """
    防止 Agent 写出 workspace 目录。
    """
    path = (WORKSPACE_DIR / relative_path).resolve()
    if not str(path).startswith(str(WORKSPACE_DIR)):
        raise ValueError(f"非法路径：{relative_path}")
    return path


def list_workspace_files() -> List[str]:
    files = []
    for path in WORKSPACE_DIR.rglob("*"):
        if path.is_file():
            files.append(str(path.relative_to(WORKSPACE_DIR)))
    return sorted(files)


def read_file(relative_path: str) -> str:
    path = safe_path(relative_path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_file(relative_path: str, content: str) -> None:
    path = safe_path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def snapshot_workspace() -> Dict[str, str]:
    """
    将当前 workspace 中的文本文件读入上下文。
    为了避免上下文过大，这里限制单文件最大 12000 字符。
    """
    result = {}
    for file in list_workspace_files():
        try:
            content = read_file(file)
            result[file] = content[:12000]
        except UnicodeDecodeError:
            result[file] = "[二进制文件，已跳过]"
    return result


def run_tests_in_workspace() -> str:
    """
    自动执行测试。
    优先 pytest；如果没有 pytest，就做一次 Python 编译检查。
    """
    if not WORKSPACE_DIR.exists():
        return "workspace 不存在"

    pytest_exists = shutil.which("pytest") is not None

    if pytest_exists:
        cmd = ["pytest", "-q"]
    else:
        cmd = ["python", "-m", "compileall", "."]

    try:
        completed = subprocess.run(
            cmd,
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (
            f"$ {' '.join(cmd)}\n\n"
            f"exit_code={completed.returncode}\n\n"
            f"STDOUT:\n{completed.stdout}\n\n"
            f"STDERR:\n{completed.stderr}"
        )
        return output
    except subprocess.TimeoutExpired:
        return "测试执行超时，已终止。"


# =========================
# 4. 通用 LLM Agent 基类
# =========================

class SimpleAgent:
    def __init__(self, name: str, system_prompt: str):
        self.name = name
        self.system_prompt = system_prompt

    def run(self, user_prompt: str, temperature: float = 0.2) -> str:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=temperature,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or ""


def extract_json(text: str) -> Any:
    """
    尽量从模型输出中提取 JSON。
    支持：
    1. 纯 JSON
    2. ```json ... ```
    3. 文本中夹杂 JSON
    """
    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    code_block = re.search(r"```json\s*(.*?)```", text, re.S)
    if code_block:
        try:
            return json.loads(code_block.group(1).strip())
        except Exception:
            pass

    json_like = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    if json_like:
        try:
            return json.loads(json_like.group(1))
        except Exception:
            pass

    raise ValueError(f"无法解析 JSON：\n{text}")


# =========================
# 5. 多 Agent 定义
# =========================

planner_agent = SimpleAgent(
    name="需求分析 Agent",
    system_prompt="""
你是一个资深软件架构师，负责把用户需求拆解成清晰的开发计划。
你必须输出 JSON，不要输出多余解释。

输出格式：
{
  "goal": "本次开发目标",
  "assumptions": ["必要假设"],
  "steps": ["开发步骤1", "开发步骤2"],
  "files_to_change": ["可能需要修改或新增的文件"],
  "test_strategy": "测试策略",
  "risk_points": ["潜在风险"]
}
"""
)

coder_agent = SimpleAgent(
    name="代码生成 Agent",
    system_prompt="""
你是一个严谨的 Python 全栈开发工程师，负责根据需求和当前仓库内容生成代码修改。
你必须输出 JSON，不要输出多余解释。

你只能通过 files 数组返回要写入的文件。
不要删除用户没有要求删除的内容。
如果是修改文件，请返回修改后的完整文件内容，而不是 diff。

输出格式：
{
  "explanation": "简要说明做了什么",
  "files": [
    {
      "path": "相对 workspace 的路径，例如 src/main.py",
      "content": "完整文件内容"
    }
  ]
}
"""
)

reviewer_agent = SimpleAgent(
    name="代码审查 Agent",
    system_prompt="""
你是一个严格的代码审查专家，负责检查代码质量、安全性、可维护性和测试结果。
你必须输出 JSON，不要输出多余解释。

输出格式：
{
  "approved": true,
  "score": 0到100之间的整数,
  "issues": [
    {
      "level": "blocker|major|minor",
      "file": "相关文件",
      "problem": "问题说明",
      "suggestion": "修复建议"
    }
  ],
  "final_comment": "总体评价"
}
"""
)

fixer_agent = SimpleAgent(
    name="自动修复 Agent",
    system_prompt="""
你是一个自动修复 Agent，负责根据测试失败信息和代码审查意见修复代码。
你必须输出 JSON，不要输出多余解释。

输出格式：
{
  "explanation": "简要说明修复了什么",
  "files": [
    {
      "path": "相对 workspace 的路径",
      "content": "修复后的完整文件内容"
    }
  ]
}
"""
)

document_agent = SimpleAgent(
    name="文档总结 Agent",
    system_prompt="""
你是技术文档工程师，负责把本次多 Agent 自动开发过程总结成清晰的中文说明。
要求：
1. 说明完成了什么
2. 说明修改了哪些文件
3. 说明测试结果
4. 说明代码审查结论
5. 给出后续优化建议

请直接输出中文 Markdown。
"""
)


# =========================
# 6. Orchestrator 编排器
# =========================

class MultiAgentOrchestrator:
    def __init__(self):
        self.messages: List[AgentMessage] = []
        self.changed_files: List[str] = []

    def add_message(self, agent: str, content: Any):
        self.messages.append(AgentMessage(agent=agent, content=content))

    def apply_file_changes(self, files: List[Dict[str, str]]):
        for item in files:
            path = item.get("path")
            content = item.get("content")
            if not path or content is None:
                continue
            write_file(path, content)
            if path not in self.changed_files:
                self.changed_files.append(path)

    def run(self, task: str, max_rounds: int = 2, should_run_tests: bool = True) -> DevTaskResponse:
        # Step 1: 需求分析
        repo_snapshot = snapshot_workspace()

        plan_prompt = f"""
用户需求：
{task}

当前仓库文件：
{json.dumps(repo_snapshot, ensure_ascii=False, indent=2)}
"""
        plan_text = planner_agent.run(plan_prompt)
        plan = extract_json(plan_text)
        self.add_message(planner_agent.name, plan)

        # Step 2: 代码生成
        code_prompt = f"""
用户需求：
{task}

开发计划：
{json.dumps(plan, ensure_ascii=False, indent=2)}

当前仓库内容：
{json.dumps(repo_snapshot, ensure_ascii=False, indent=2)}

请生成代码修改。
"""
        code_text = coder_agent.run(code_prompt)
        code_result = extract_json(code_text)
        self.add_message(coder_agent.name, code_result)

        self.apply_file_changes(code_result.get("files", []))

        test_output = None
        review_result = None
        success = False
        rounds_used = 1

        # Step 3: 测试 + 审查 + 自动修复循环
        for round_idx in range(1, max_rounds + 1):
            rounds_used = round_idx

            current_snapshot = snapshot_workspace()

            if should_run_tests:
                test_output = run_tests_in_workspace()
            else:
                test_output = "用户选择跳过测试。"

            self.add_message("测试执行 Agent", test_output)

            review_prompt = f"""
用户需求：
{task}

开发计划：
{json.dumps(plan, ensure_ascii=False, indent=2)}

当前仓库内容：
{json.dumps(current_snapshot, ensure_ascii=False, indent=2)}

测试输出：
{test_output}

请进行代码审查。
"""
            review_text = reviewer_agent.run(review_prompt)
            review_result = extract_json(review_text)
            self.add_message(reviewer_agent.name, review_result)

            approved = bool(review_result.get("approved"))
            score = int(review_result.get("score", 0))

            if approved and score >= 80:
                success = True
                break

            if round_idx >= max_rounds:
                break

            # Step 4: 自动修复
            fix_prompt = f"""
用户需求：
{task}

当前仓库内容：
{json.dumps(current_snapshot, ensure_ascii=False, indent=2)}

测试输出：
{test_output}

代码审查结果：
{json.dumps(review_result, ensure_ascii=False, indent=2)}

请修复问题，返回需要修改的完整文件。
"""
            fix_text = fixer_agent.run(fix_prompt)
            fix_result = extract_json(fix_text)
            self.add_message(fixer_agent.name, fix_result)

            self.apply_file_changes(fix_result.get("files", []))

        # Step 5: 文档总结
        summary_prompt = f"""
用户需求：
{task}

变更文件：
{json.dumps(self.changed_files, ensure_ascii=False, indent=2)}

测试输出：
{test_output}

代码审查结果：
{json.dumps(review_result, ensure_ascii=False, indent=2)}

全部 Agent 消息：
{json.dumps([m.model_dump() for m in self.messages], ensure_ascii=False, indent=2)}
"""
        summary = document_agent.run(summary_prompt, temperature=0.3)
        self.add_message(document_agent.name, summary)

        return DevTaskResponse(
            task=task,
            success=success,
            rounds=rounds_used,
            messages=self.messages,
            changed_files=self.changed_files,
            test_output=test_output,
            review_result=review_result,
            summary=summary,
        )


# =========================
# 7. API 路由
# =========================

@app.get("/")
def root():
    return {
        "message": "Multi-Agent Dev Automation System is running.",
        "workspace": str(WORKSPACE_DIR),
        "model": OPENAI_MODEL,
        "endpoints": {
            "run_task": "POST /tasks/run",
            "files": "GET /workspace/files",
            "read_file": "GET /workspace/files/{path}",
        },
    }


@app.get("/workspace/files")
def get_workspace_files():
    return {
        "workspace": str(WORKSPACE_DIR),
        "files": list_workspace_files(),
    }


@app.get("/workspace/files/{file_path:path}")
def get_workspace_file(file_path: str):
    try:
        return {
            "path": file_path,
            "content": read_file(file_path),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/tasks/run", response_model=DevTaskResponse)
def run_dev_task(request: DevTaskRequest):
    try:
        orchestrator = MultiAgentOrchestrator()
        return orchestrator.run(
            task=request.task,
            max_rounds=request.max_rounds,
            should_run_tests=request.run_tests,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =========================
# 8. 本地命令行运行
# =========================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run multi-agent dev task from CLI.")
    parser.add_argument("task", type=str, help="研发任务描述")
    parser.add_argument("--rounds", type=int, default=2, help="最大修复轮数")
    parser.add_argument("--no-tests", action="store_true", help="跳过测试")

    args = parser.parse_args()

    orchestrator = MultiAgentOrchestrator()
    result = orchestrator.run(
        task=args.task,
        max_rounds=args.rounds,
        should_run_tests=not args.no_tests,
    )

    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
