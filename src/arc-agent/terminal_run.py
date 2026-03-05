import asyncio
import os
import sys
import argparse
import yaml
from colorama import init, Fore, Style
from typing import Dict, Any

# Add current directory to sys.path to ensure modules can be imported
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

from agent_workflow import run_agent_workflow
from utils import get_all_leaves, topological_sort

# Initialize colorama
init()

def print_logo():
    logo = f"""
{Fore.CYAN}
    _    ____   ____ 
   / \  |  _ \ / ___|
  / _ \ | |_) | |    
 / ___ \|  _ <| |___ 
/_/   \_\_| \_\\____|
{Style.RESET_ALL}
{Fore.BLUE}Agentic Requirement Compiler - Terminal Runner{Style.RESET_ALL}
    """
    print(logo)

async def terminal_log_callback(payload: Dict[str, Any]):
    """
    Callback function for printing logs to the terminal.
    Formats the output based on the agent and message type.
    """
    agent = payload.get("agent", "System")
    message = payload.get("message", "")
    node_id = payload.get("nodeId", "")
    status = payload.get("status", "")

    timestamp = "" # Could add timestamp if needed
    
    agent_color = Fore.WHITE
    if agent == "RequirementAnalyzer":
        agent_color = Fore.YELLOW
    elif agent == "InterfaceDesigner":
        agent_color = Fore.MAGENTA
    elif agent == "TestGenerator":
        agent_color = Fore.GREEN
    elif agent == "TestDrivenDeveloper":
        agent_color = Fore.CYAN
    elif agent == "System":
        agent_color = Fore.RED

    prefix = f"{Fore.WHITE}[{node_id}]{Style.RESET_ALL} " if node_id else ""
    
    print(f"{prefix}{agent_color}[{agent}]{Style.RESET_ALL} {message}")
    
    if status:
        print(f"{prefix}{Fore.BLUE}Status Update: {status}{Style.RESET_ALL}")

async def run_project(workspace_path: str):
    """
    Runs the agent workflow for all requirements in the specified workspace.
    """
    requirements_path = os.path.join(workspace_path, 'requirements', 'requirements.yaml')
    
    if not os.path.exists(requirements_path):
        print(f"{Fore.RED}Error: Requirements file not found at {requirements_path}{Style.RESET_ALL}")
        return

    print(f"{Fore.GREEN}Loading requirements from {requirements_path}...{Style.RESET_ALL}")
    
    try:
        with open(requirements_path, 'r', encoding='utf-8') as f:
            req_data = yaml.safe_load(f)
    except Exception as e:
        print(f"{Fore.RED}Error parsing requirements file: {e}{Style.RESET_ALL}")
        return

    # Simple topological sort simulation (reuse logic from main.py if possible, or simplified here)
    # Assuming get_all_leaves and topological_sort are available in utils.py as per context
    try:
        leaves = get_all_leaves(req_data)
        process_queue = topological_sort(leaves)
        
        print(f"{Fore.GREEN}Processing Order: {', '.join(process_queue)}{Style.RESET_ALL}\n")
        
        # In terminal mode, we might want to process sequentially or parallel
        # For clarity in logs, sequential is better
        for node_id in process_queue:
            print(f"{Fore.WHITE}{'='*60}{Style.RESET_ALL}")
            print(f"{Fore.YELLOW}Processing Node: {node_id}{Style.RESET_ALL}")
            print(f"{Fore.WHITE}{'='*60}{Style.RESET_ALL}")
            
            # Find requirement data for this node (simplified lookup)
            # In a real app, you'd have a better lookup map. 
            # Here we just pass the whole dict or specific part if we had a helper.
            # Assuming run_agent_workflow handles lookup or we pass empty and let it read file?
            # main.py passes `requirement_data or {}`. 
            # Ideally we should find the specific node data. 
            # For now, let's pass the whole req_data and let agent handle or just empty dict if agent reads file.
            # Looking at agent_workflow.py/RequirementAnalyzer, it seems to rely on the passed data.
            # Let's create a simple finder or pass empty if not easily available.
            
            # NOTE: For this implementation, we'll assume the agent can read the file 
            # or we pass basic info.
            node_data = {} 
            # Attempt to find node data in the tree (BFS)
            queue = [req_data]
            while queue:
                curr = queue.pop(0)
                if curr.get('id') == node_id:
                    node_data = curr
                    break
                queue.extend(curr.get('children', []))
            
            await run_agent_workflow(
                node_id=node_id, 
                requirement_data=node_data,
                broadcast_cb=terminal_log_callback
            )
            
            print(f"\n{Fore.GREEN}✓ Node {node_id} Completed{Style.RESET_ALL}\n")

    except Exception as e:
        print(f"{Fore.RED}Execution failed: {e}{Style.RESET_ALL}")
        import traceback
        traceback.print_exc()

def main():
    parser = argparse.ArgumentParser(description="ARC Terminal Runner")
    parser.add_argument("workspace", help="Path to the workspace directory containing requirements/requirements.yaml")
    
    args = parser.parse_args()
    workspace_path = os.path.abspath(args.workspace)
    
    print_logo()
    print(f"Workspace: {workspace_path}")
    print("-" * 50)
    
    if not os.path.exists(workspace_path):
        print(f"{Fore.RED}Error: Workspace path does not exist.{Style.RESET_ALL}")
        return

    # Set global workspace root if needed by tools (simulating main.py behavior)
    # Some tools might rely on a global or env var for root path.
    # We should set cwd to workspace or handle it.
    os.chdir(workspace_path)
    
    asyncio.run(run_project(workspace_path))

if __name__ == "__main__":
    main()
