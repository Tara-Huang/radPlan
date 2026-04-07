import io
import json
import asyncio, os, copy
import inspect
import logging
import pickle
import re
import textwrap
import time
import pandas as pd
from termcolor import colored
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from warnings import warn
from autogen.agentchat import Agent, ConversableAgent 
from autogen.oai.client import OpenAIWrapper
from autogen.token_count_utils import count_token, get_max_token_limit, num_tokens_from_functions
# from autogen.agentchat.contrib.capabilities.text_compressors import LLMLingua
# from autogen.agentchat.contrib.capabilities.transforms import TextMessageCompressor
import google.generativeai as genai
from radplan_qwen.before.prompts import get_prompt
from use_portpy import IMRT, JSON2Markdown 
import numpy as np

# llm_lingua = LLMLingua(dict(model_name="microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank", use_llmlingua2=True, device_map="cuda:0"))
# text_compressor = TextMessageCompressor(text_compressor=llm_lingua, compression_params={"target_token":100, "keep_first_sentence":1}, cache=None)

# genai.configure(api_key='AIzaSyABK3x5mMVuZrqS9HIUgn13rmVXgZYDm0E')
# for m in genai.list_models():
#   if 'generateContent' in m.supported_generation_methods:
#     print(m.name)
# gemini_model = genai.GenerativeModel('gemini-1.5-pro-latest')

def md2df(md_table):
    if 'OptPara' in md_table:
        # remove the first line 
        md_table = '\n'.join(md_table.split('\n')[1:])

    # Use pandas to read the Markdown table
    df = pd.read_table(io.StringIO(md_table), sep='|', skipinitialspace=True)
    # Clean up the column names and drop any empty columns/rows
    df.columns = df.columns.str.strip()
    df = df.dropna(axis=0, how='all')
    df = df.dropna(axis=1, how='all')
    # Remove the separator row (usually the second row in a markdown table)
    df = df.drop(df.index[df.iloc[:, 0].str.contains('-{3,}', na=False)]).reset_index(drop=True)
    # Remove any remaining rows that are all NaN or contain only separators
    df = df[~df.apply(lambda row: row.astype(str).str.contains('-{3,}').all() or row.isna().all(), axis=1)]
    # Reset the index after dropping rows
    df = df.reset_index(drop=True)
    return df

def check_no_errors(text):
    pattern = r'(?i)no\s+(?:\w+\s+){0,5}errors'
    match = re.search(pattern, text, re.IGNORECASE)
    return bool(match)

def check_OptPara(text):
    pattern = r'(?m)^(?=.*\|.*ROI Name.*\|)(?:.*\|.*\n){5,}'
    return bool(re.search(pattern, text))

def extract_OptPara(iter_count, response: str):
    # extract the OptPara part from the modified response
    # OptPara = re.search(r'\|.*?ROI Name.*?\|.*?(\n\|.*?\|.*?\|.*?\|.*?\|.*?\|)+', response, re.DOTALL)
    # OptPara = re.search(r'\|.*?ROI Name.*?(?:\n\|.*+)+', response, re.DOTALL)
    # OptPara = re.search(r'\|\s*ROI Name.*?\|\s*Weight\s*\|\n(?:\|(?:[^|]*\|){4,}\n)+(?=\n[^|]|\Z)',response, re.MULTILINE|re.DOTALL)
    # if OptPara is not None:
    #     OptPara = OptPara.group()
    # else:
    #     raise ValueError("OptPara not found in the dosimetrist's response.")

    lines = response.split('\n')
    table_start = None
    table_end = None

    # lines = [line for line in lines if ':---' not in line.strip()]
    lines = [line for line in lines if '```' not in line.strip()]

    for i, line in enumerate(lines):
        if '|' in line and 'ROI Name' in line:
            table_start = i
        elif table_start is not None:
            if line.strip() == '':  # If we've reached an empty line
                table_end = i
                break
            # if reached an line that doesn't have a '|' character, then it's end of the table
            elif '|' not in line.strip():  
                table_end = i
                break
            elif i == len(lines) - 1:  # If we've reached the last line
                table_end = i + 1  # Include the last line
                break
   
    if table_start is not None and table_end is not None:
        # Strip each line to handle potential leading/trailing spaces from LLM
        OptPara = '\n'.join([line.strip() for line in lines[table_start:table_end]])
    else:
        raise ValueError("OptPara not found in the dosimetrist's response.")

    final_response = f'### OptPara Iter-{iter_count}\n' + OptPara

    # print the rest of the response after removing the OptPara part
    # rest_of_response = response.replace(OptPara, '') 
    # print(rest_of_response)

    return final_response

def add_iter_to_OptPara(iter_count, response):
    lines = response.split('\n')

    # Remove any existing iteration numbers 
    lines = [line for line in lines if '### OptPara Iter' not in line]  

    # Add the iteration number to the OptPara table
    for i, line in enumerate(lines):
        if 'ROI Name' in line and '|' in line:
            # insert the iteration number to the previouse line
            lines.insert(i, f'### OptPara Iter-{iter_count}') 
            break
            
    return '\n'.join(lines)

def parse_traj(traj_file_path, iter_idx, role):
    """
    Parses the trajectory file to return either the OptPara table or the Dosimetric Outcomes table
    based on the specified role ('TPS' for OptPara table, 'dosimetric' for Dosimetric Outcomes table) at specific iter_index
    
    Parameters:
    - traj_file_path: Path to the trajectory file.
    - iter_index: Iteration index to identify the specific iteration's data.
    - role: 'TPS' to return the OptPara table, 'dosimetric' to return the Dosimetric Outcomes table.
    
    Returns:
    - the parsed md string containing the requested table.
    """
    # Read the markdown file
    with open(traj_file_path, 'r') as file:
        content = file.read()
    
    # Define the section start based on iter_index and role
    if role.lower() == 'dosimetric':
        section_start = f"### OptPara Iter-{iter_idx}"
    elif role.lower() == 'tps':
        section_start = f"Given OptPara Iter-{iter_idx}"
    else:
        raise ValueError("Invalid role specified. Choose either 'TPS' or 'dosimetric'.")
    
    # Find the start of the section
    start_index = content.find(section_start)
    if start_index == -1:
        print("Section not found.")
        return None
    
    # Extract the section until the next section starts or file ends
    end_index = content.find("\n\n", start_index + len(section_start))
    if end_index == -1:  # This means we are at the last section
        end_index = len(content)
    
    # Extract and return the section
    return content[start_index:end_index].strip()

def simplify_messages(self, sender, messages):
    '''for dosimetrist and physicist'''
    if sender.iter_count>1 and (sender.iter_count-1)%sender.everyN==0:  # every 10 iterations
        for msg in messages[1:]:  # skip first message which is group introduction msg and without name key
            speaker = msg.get('name', self._name)
            if any(role in speaker for role in ['TPS_proxy', 'dosimetrist', 'physicist', 'human_supervisor']):
                messages.remove(msg)

        assert sender.traj != '', "Trajectory Summary is empty."
        messages.append({'role': 'user', 'name': 'optim_trajectoy_reviwer', 'content':sender.traj})


def dosimetrist_preprocess(self, sender, messages):
    if sender.iter_count == 1 or sender.is_skip_physicist:
        return ''

    # 1. 安全提取各个角色的最后一条消息
    dosimetrist_msg = next((msg for msg in reversed(messages) if msg.get('name') == 'dosimetrist'), {})
    tps_msg = next((msg for msg in reversed(messages) if msg.get('name') == 'TPS_proxy'), {})
    physicist_msg = next((msg for msg in reversed(messages) if msg.get('name') == 'physicist'), {})
    supervisor_msg = next((msg for msg in reversed(messages) if msg.get('name') == 'human_supervisor'), {})

    # 提取物理师的优先级评估
    dose_eval = ""
    if physicist_msg and 'Optimization Priorities' in physicist_msg.get('content', ''):
        dose_eval = physicist_msg['content'].split('Optimization Priorities')[1]
    else:
        dose_eval = physicist_msg.get('content', '')

    # 2. 合并背景信息
    combined_content = (
        f"--- Previous Dosimetrist Proposal ---\n{dosimetrist_msg.get('content', 'None')}\n\n"
        f"--- TPS Outcomes ---\n{tps_msg.get('content', 'None')}\n\n"
        f"--- Physicist Optimization Priorities ---\n{dose_eval}\n"
    )
    
    if supervisor_msg.get('content') and supervisor_msg.get('content') != 'No comments.':
        combined_content += f"\n--- Human Supervisor Feedback ---\n{supervisor_msg.get('content')}\n"

    base_msg_to_send = [{'role': 'user', 'content': combined_content}]

    suggestedAdjustment = ""
    reflection_history = [] 

    for i in range(3):
        # 3. 向提议代理 (SuggestDosimetrist) 请求建议
        current_request_msgs = base_msg_to_send + reflection_history
        try:
            suggestedAdjustment_response = self.agent_SuggestDosimetrist.generate_reply(messages=current_request_msgs)
        except Exception:
            suggestedAdjustment = ""
            break
        if suggestedAdjustment_response is None:
            suggestedAdjustment = ""
            break
        if isinstance(suggestedAdjustment_response, dict):
            suggestedAdjustment = str(suggestedAdjustment_response.get('content') or "")
        else:
            suggestedAdjustment = str(suggestedAdjustment_response)
        if not suggestedAdjustment.strip():
            break
        
        # 4. 【核心修复】向审查代理 (Check) 发送请求时，必须把提议作为 User 消息发送！
        # 绝不能以 'assistant' 结尾去请求 API。
        check_msg_content = (
            f"{combined_content}\n\n"
            f"--- Proposed Adjustment to Evaluate ---\n{suggestedAdjustment}\n\n"
            f"Please review the above proposed adjustment."
        )
        check_request_msgs = [{'role': 'user', 'content': check_msg_content}]
        
        try:
            critique_response = self.agent_SuggestDosimetristCheck.generate_reply(messages=check_request_msgs)
        except Exception:
            break
        if critique_response is None:
            break
        if isinstance(critique_response, dict):
            critique_text = str(critique_response.get('content') or "")
        else:
            critique_text = str(critique_response)
        
        if 'no errors' in critique_text.lower():
            break
        else:
            # 记录反思历史，给提议代理 (SuggestDosimetrist) 看的，严格保持 assistant -> user 交替
            reflection_history.append({'role': 'assistant', 'content': suggestedAdjustment})
            reflection_history.append({'role': 'user', 'content': f"Critique on your previous suggestion:\n{critique_text}\nPlease provide an updated suggestion."})
    
    extra_msg = """\
Note:
- The "All Possible OptPara Adjustments" list all possible adjustments for each requirement, the dosimetrist does not need to use all of them. The dosimetrist should select the most appropriate adjustments based on their own judgment.
- If D95 < 60 Gy, avoid increasing PTV quadratic-overdose Weight and PTV quadratic-underdose Weight simultaneously to prevent compromising the PTV coverage.
- If D95 < 60 Gy, never decrease PTV quadratic-overdose Target Gy because it will punish the dose points above the target dose.
- If Max Dose for a struct is not met the goal, decreasing the max_dose "Target Gy" for the struct is a VERY EFFECTIVE WAY to reduce the max dose. 
"""
    suggestedAdjustment += f'\n\n{extra_msg}'
    return suggestedAdjustment

def generate_oai_reply_with_process_dosimetrist(
    self: ConversableAgent,
    messages: Optional[List[Dict]] = None,
    sender: Optional[Agent] = None,
    config: Optional[OpenAIWrapper] = None,
) -> Tuple[bool, Union[str, Dict, None]]:
    """Generate a reply using autogen.oai."""
    client = self.client if config is None else config
    if client is None:
        return False, None
    if messages is None:
        messages = self._oai_messages[sender]  # sender always is the group manager

    assert self._name == "dosimetrist"

    # try cache
    if not sender.traj_file is None:
        OptPara = parse_traj(sender.traj_file, sender.iter_count, 'dosimetric')
        if OptPara is not None:
            simplify_messages(self, sender, messages)
            return True, OptPara

    premsg = dosimetrist_preprocess(self, sender, messages)
    print(colored(premsg, 'cyan'))
    msg_to_send = messages if premsg == '' else messages + [{'role': 'user', 'content': premsg}]
    response = self._generate_oai_reply_from_client(client, self._oai_system_message+msg_to_send,  self.client_cache)
    response = response['content'] if isinstance(response, dict) else response
    response = dosimetrist_post_process(self, sender.iter_count, messages, response, client)
    response = add_iter_to_OptPara(sender.iter_count, response)
    # OptPara = dosimetrist_extract_OptPara(sender.iter_count, response)

    # simplify_physicist_msgs(self, messages, -2)  # keep only takeaways in dosimetrist's chat history after reply
    simplify_messages(self, sender, messages)
    return True, response 

def dosimetrist_post_process(
    self: ConversableAgent,
    cur_iter: int,
    messages: Optional[List[Dict]] = None,
    response: str = None,
    client: Optional[OpenAIWrapper] = None,
) -> Union[str, Dict, None]:
    '''Dosimetrist self-reflection and correction'''

    # refine the OptPara with critique for maximum 6 times 
    for i in range(6): 
        print(colored(f'dosimetrist response-{i}', 'red'))
        print(colored(response, 'blue'))

        error = False
        try:
            # check the exsiting of OptPara
            if not check_OptPara(response):
                raise ValueError(f"OptPara Iter-{cur_iter} not found in your response. You are required to provide OptPara markdown table in your response.") 
                
            # multiple OptPara fractions in the response
            # if response.count('ROI Name') > 1:
            #     raise ValueError(f"Multiple OptPara fractions found in the response. Please provide only one complete OptPara Iter-{cur_iter}.")
            
            # ensure all ROIs and Type are valid
            valid_ROIs =  ['GTV', 'PTV', 'LUNGS_NOT_GTV', 'LUNG_L', 'LUNG_R', 'ESOPHAGUS', 'HEART', 'CORD', 'SKIN', 'RIND_0', 'RIND_1', 'RIND_2', 'RIND_3', 'RIND_4', 'NA']
            valid_objs = [ 'quadratic-overdose', 'quadratic-underdose', 'quadratic', 'linear-overdose', 'smoothness-quadratic', 'max_dose', 'mean_dose', 'dose_volume_V' ]
            OptPara_md = extract_OptPara('tmp', response)
            df = md2df(OptPara_md)
            cur_ROIs = [str(s).strip() for s in df['ROI Name'].tolist()]
            cur_types = [str(s).strip() for s in df['Objective Type'].tolist()]
            n_ptv_quad_overdose = sum([1 for i, row in df.iterrows() if 'PTV' in row['ROI Name'] and 'quadratic-overdose' in row['Objective Type']])
            n_ptv_quad_underdose = sum([1 for i, row in df.iterrows() if 'PTV' in row['ROI Name'] and 'quadratic-underdose' in row['Objective Type']])

            # find valid ROIs that are not in the ROIs
            not_found_ROIs = [s for s in valid_ROIs[0:-1] if s not in cur_ROIs]  # exclude 'NA'
            if len(not_found_ROIs) > 0:
                raise ValueError(f"The following ROI names are missing in your response: {not_found_ROIs}. You should include all these ROIs in your OptPara table.") 

            # ensure 'smoothness-quadratic' is in the OptPara
            if 'smoothness-quadratic' not in cur_types:
                raise ValueError(f"The 'smoothness-quadratic' Objective Type is missing in your response. You should include it in your OptPara table. Note that the ROI Name should be 'NA' for this Objective Type.")

            # find OARs/types are not in valid_OARs/objs
            invalid_ROIs = [s for s in cur_ROIs if s not in valid_ROIs]
            if len(invalid_ROIs) > 0:
                raise ValueError(f"The following ROI names are invalid: {invalid_ROIs}. The available ROIs are: {valid_ROIs}.")

            invalid_types = [s for s in cur_types if s not in valid_objs]
            if len(invalid_types) > 0:
                raise ValueError(f"The following Objective Types are invalid: {invalid_types}. The available Objective Types are: {valid_objs}.")

            # weight and volume parameter errors
            for i, row in df.iterrows():
                struct = str(row['ROI Name']).strip()
                obj_type = str(row['Objective Type']).strip()
                # 容错处理 Target Gy
                empty_placeholders = ['NA', 'N/A', '-', '', 'NONE']
                target_gy_str = str(row['Target Gy']).strip()
                target_gy = 0
                if target_gy_str.upper() not in empty_placeholders:
                    # 移除非数字字符以防 LLM 加上单位，比如 "66.0 Gy"
                    num_match = re.search(r"[-+]?\d*\.\d+|\d+", target_gy_str)
                    if num_match:
                        target_gy = float(num_match.group())

                vol = str(row['% Volume']).strip()
                weight = str(row['Weight']).strip()

                if 'PTV' in struct and 'quadratic-overdose'in obj_type and target_gy < 60 and n_ptv_quad_overdose == 1:
                    raise ValueError(f"The PTV quadratic-overdose have a Target Gy less than 60 Gy, which will punish the dose points above {target_gy} Gy. Please adjust the Target Gy to be greater than 60 Gy or give a rationale for the lower Target Gy.")

                if 'PTV' in struct and 'quadratic-underdose'in obj_type and target_gy < 60 and n_ptv_quad_underdose == 1:
                    raise ValueError(f"The PTV quadratic-underdose have a Target Gy less than 60 Gy, which will only punish the dose points below {target_gy} Gy. Please adjust it or give a rationale for the lower Target Gy.")

                if vol != 'NA' and '%' in vol:
                    raise ValueError(f"The % Volume parameter {vol} should not have a % symbol.")
                
                if obj_type in ['max_dose', 'mean_dose', 'dose_volume_V'] and weight.upper() not in empty_placeholders:
                    raise ValueError(f"The {obj_type} is an optimization constraint and should not have a Weight parameter. (You provided: '{weight}')")

                if obj_type in ['quadratic-overdose', 'quadratic-underdose', 'linear-overdose', 'quadratic', 'linear', 'smoothness-quadratic'] and weight.upper() in empty_placeholders:
                    raise ValueError(f"The {obj_type} is an optimization objective and MUST have a numerical Weight parameter.")

        except Exception as e:
            error = True
            print(colored(f"An error occurred: {str(e)}", 'red'))
            
            # 给出更清晰的 LLM 修改指导
            if 'could not convert string to float' in str(e):
                critique = f'Error: "Target Gy", "% Volume", and "Weight" must be concrete numbers (e.g., 60), NOT formulas like "1.1*prescription_gy" or strings. Please fix it and generate OptPara Iter-{cur_iter} again.'
            elif 'float' in str(e):
                critique = f'The OptPara markdown table format is corrupted or missing numeric values. Please ensure clean markdown formatting and generate OptPara Iter-{cur_iter} again.'
            else:
                critique = f'Errors or Warnings: {str(e)}. Please correct your parameters and generate OptPara Iter-{cur_iter} again.'
            # import pdb; pdb.set_trace()
            # print(f"debug .....")
            # re-generate the OptPara

            # sleep 5 minutes to avoid openai api rate limit
            # print(f"Sleeping for 1 minutes...")
            # time.sleep(1*60)

            msgs_to_send = messages + [ {'role': 'assistant', 'content': response}, {'role': 'user', 'content': critique} ] 
            response = self._generate_oai_reply_from_client(client, self._oai_system_message+msgs_to_send, self.client_cache)
            response = response['content'] if isinstance(response, dict) else response
            if 'Iter-' in response:
                response = re.sub(r"Iter-\d+", "", response)  # 加上 + 号，支持多位数迭代 

        compare = ''
        if False and len(invalid_ROIs)==0 and len(invalid_types)==0 and cur_iter>1 :
            # 2 compare OptPara versions
            prev_optPara = next((msg for msg in reversed(messages) if msg.get('role') == 'assistant'), None)
            assert prev_optPara is not None, "Previous OptPara not found in the chat history."
            prev_optPara = prev_optPara['content']
            msgs_to_send = [{'role': 'user', 'content': f'{prev_optPara}\n\nBelow is OptPara Iter-{cur_iter}:\n{response}'}]
            compare = self.agent_compareDosimetrist.generate_reply(msgs_to_send)['content']
            compare = f'\n\nBelow is the changes between OptPara Iter-{cur_iter-1} and Iter-{cur_iter}:\n{compare}'
            print(colored(compare, 'blue'))

        if False and cur_iter > 1:
            # 3 critique
            # prepare the msg history for critique. Only keep last two msgs: physicist's evaluation and human supervisor's feedback
            msgs = copy.deepcopy(messages)[-2:]
            assert msgs[-1]['name'] == 'human_supervisor' and msgs[-1]['role'] == 'user', "Human Supervisor's feedback not found in the chat history."
            assert msgs[-2]['name'] == 'physicist' and msgs[-2]['role'] == 'user', "Physicist's evaluation not found in the chat history."
            # let gemini know the roles of physicist and huamn supervisor
            msgs[-2]['content'] = f"\nBelow is Physicist Evaluation for OptPara Iter-{cur_iter}:\n" + msgs[-2]['content']
            msgs[-1]['content'] = f"\nBelow is Huamn Supervisor feedback for OptPara Iter-{cur_iter}:\n" + msgs[-1]['content']

            msgs_to_send = msgs + [{'role': 'user', 'content': f"Please review OptPara Iter-{cur_iter}:**\n{response}\n{compare}"}]
            #critique.append(self.agent_ctriticalDosimetrist.generate_reply(msgs_to_send)['content'])
            #print('\n\n', colored(critique[-1], 'blue'))
            
            critique = self.agent_ctriticalDosimetrist.generate_reply(msgs_to_send)['content']
            print('\n\n', colored(critique, 'blue'))
            
        if not error:
            break
    return response


def generate_oai_reply_with_process_physicist(
    self: ConversableAgent,
    messages: Optional[List[Dict]] = None,
    sender: Optional[Agent] = None,
    config: Optional[OpenAIWrapper] = None,
) -> Tuple[bool, Union[str, Dict, None]]:
    """Generate a reply using autogen.oai."""
    client = self.client if config is None else config
    if client is None:
        return False, None
    if messages is None:
        messages = self._oai_messages[sender]  # sender always is the group manager

    assert self._name == 'physicist'

    # physicist_compare_with_protocol(self, messages)
    if not sender.is_skip_physicist:
        max_retries = 2
        last_error = None
        response = None
        for attempt in range(max_retries + 1):
            try:
                response = self._generate_oai_reply_from_client(client, self._oai_system_message + messages, self.client_cache)
                response = response['content'] if isinstance(response, dict) else response
                response = physicist_remove_repeat_phrase(response)
                response = physicist_post_process(self, messages, sender, response, client)
                last_error = None
                break
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    backoff_s = 2 ** attempt
                    print(colored(f"Physicist LLM call failed (attempt {attempt + 1}/{max_retries + 1}): {e}", 'red'))
                    time.sleep(backoff_s)
                else:
                    break

        if last_error is not None:
            response = (
                "### Optimization Priorities\n"
                "- LLM timeout/error occurred; keep the latest feasible OptPara and continue conservatively.\n"
                "- Prioritize PTV coverage and cord safety; avoid aggressive new constraints this round."
            )

        # sleep at the end of the iteration
        if sender.optim_time < sender.iter_sleep:  # sleep 5 minutes to avoid openai api rate limit
            print(f"Sleeping for {sender.iter_sleep - sender.optim_time} seconds...")
            time.sleep(sender.iter_sleep - sender.optim_time)

    else:
        response = 'No comments.'

    physicist_save_trajectory(self, sender, messages)
    simplify_messages(self, sender, messages)

    termination_markers = [
        "TERMINATE:",
        "WORKFLOW TERMINATED",
        "PROCESS TERMINATED",
        "PROCESS COMPLETE",
        "NO FURTHER OPTIMIZATION",
        "NO FURTHER ITERATIONS REQUIRED",
    ]
    current_score = sender.reward_history[-1] if hasattr(sender, 'reward_history') and sender.reward_history else None
    response_upper = str(response).upper()
    should_terminate = (current_score is not None and current_score >= 100.0) or any(marker in response_upper for marker in termination_markers)

    if not should_terminate:
        response += f"\n\nNext, the dosimetrist should propose OptPara Iter-{sender.iter_count+1}."
        sender.iter_count += 1

    return True, response

def physicist_save_trajectory(self, sender, messages):
    '''return the optimization trajectories'''
    cur_iter = sender.iter_count
    if cur_iter > 0:
        dosimetrist_msg = next((msg for msg in reversed(messages) if msg.get('name') == 'dosimetrist'), None)
        tps_msg = next((msg for msg in reversed(messages) if msg.get('name') == 'TPS_proxy'), None)
        assert tps_msg is not None and dosimetrist_msg is not None, "TPS or dosimetrist's message not found in the chat history."

        traj_folder = f'{sender.traj_folder}/{sender.pid}'
        if not os.path.exists(traj_folder):
            os.makedirs(traj_folder)

        # save the full optimization trajectories
        OptPara = extract_OptPara(cur_iter, dosimetrist_msg['content'])
        doseOut = tps_msg['content']
        new_traj = f"OptPara Iter-{cur_iter}:\n{OptPara}\n\nDosimetric OutComes:\n{doseOut}"
        sender.traj_full.append(new_traj)
        sender.traj_everyN.append(new_traj)
        with open(f'{traj_folder}/optim_trajectores_full.md', 'w') as f:
            f.write('\n\n'.join(sender.traj_full))

        # summarize the optimization trajectories
        if cur_iter%sender.everyN==0:  # every 10 iterations
            prefix = f'Now, analyze the trajectory from Iter-{cur_iter-sender.everyN+1} to Iter-{cur_iter}:\n'
            content = prefix + '\n\n'.join(sender.traj_everyN)  # current N traj
            sender.traj = self.agent_trajReviewer.generate_reply(messages=[{'role': 'user', 'content':content}])
            sender.traj = sender.traj['content'] if isinstance(sender.traj, dict) else sender.traj
            # traj_verf = self.agent_TrajVef.generate_reply(messages=[{'role': 'user', 'content':sender.traj}])
            # traj_verf = traj_verf['content'] if isinstance(traj_verf, dict) else traj_verf 
            # sender.traj += '\n\n' + traj_verf

            sender.traj_everyN = []  # reset
            with open(f'{traj_folder}/optim_trajectores.md', 'w') as f:
                f.write(sender.traj)
            print(colored(sender.traj, 'yellow'))

def physicist_compare_with_protocol(self, messages):
    dosimetric_outcomes = messages[-1]['content']
    comp_protocol = self.agent_comparePhysicist.generate_reply([messages[-1]])['content'] 
    comp_protocol = comp_protocol['content'] if isinstance(comp_protocol, dict) else comp_protocol
    print(colored(comp_protocol, 'blue'))
    # modify messages inplace to include comparision
    # messages[-1]['content'] = dosimetric_outcomes + '\n\n Protocol Adherence:\n' + comp_protocol

def physicist_post_process(
    self: ConversableAgent,
    messages: Optional[List[Dict]] = None,
    sender: Optional[Agent] = None,
    response: str = None,
    client: Optional[OpenAIWrapper] = None,
) -> Union[str, Dict, None]:
    '''physicist self-reflection and correction'''

    critique = 'no errors'
    for i in range(3): 
        # print(colored(f'phycisist response-{i}', 'red'))
        # print(colored(response, 'blue'))

        # check the exsiting of Optimization Priorities
        is_missing = False if 'Optimization Priorities' in response else True
        if is_missing:
            critique = f'ERRORs: "### Optimization Priorities" is missing in your response! Please add this section to your response.'
            print(colored(critique, 'red'))
        
            msgs_to_send = messages + [ {'role': 'assistant', 'content': response}, {'role': 'user', 'content': critique} ] 
            response = self._generate_oai_reply_from_client(client, self._oai_system_message+msgs_to_send, self.client_cache)
        else:
            break

    return response

def physicist_remove_repeat_phrase(response):
    '''remove repeat phrase in response'''
    # split the response into lines
    lines = response.split('\n')
    lines = [l for l in lines if 'Next, the dosimetrist should propose OptPara' not in l]
    new_response = '\n'.join(lines)
    return new_response

def _simplify_physicist_msgs(self, messages, start_idx):
    """Compress the message history to keep only the key takeaways for all agents."""
    for msg in messages[start_idx:]:
        # Note: a agent's own msg in messages has role as assistant and name as None, so we get the agent owes name from self._name 
        speaker = msg.get('name', self._name)

        if 'physicist' in speaker or 'oncologist' in speaker:
            # keep only the content between 'begin_phrase' and "Now, the dosimetrist should propose OptPara"
            if 'Key Takeaways' in msg['content']:
                begin_phrase = 'Key Takeaways' 
            elif 'Optimization Priorities' in msg['content']:
                begin_phrase = 'Optimization Priorities'
            else:
                continue

            begin_idx = msg['content'].find(begin_phrase)
            end_idx = msg['content'].find('Now, the dosimetrist should propose OptPara')
            msg['content'] = msg['content'][begin_idx:end_idx]

def calculate_rl_reward(eval_df):
    """
    Calculate a comprehensive reward score based on dosimetric outcomes.
    Includes negative penalties for hard limit violations, clipping for infs,
    and a pre-check to prevent "dose collapse" from being rewarded.
    """
    if eval_df is None:
        return -500.0, "[WARN] Empty evaluation dataframe (None)."
    if not hasattr(eval_df, "iterrows"):
        return -500.0, f"[WARN] Invalid evaluation dataframe type: {type(eval_df)}"
    if len(eval_df) == 0:
        return -500.0, "[WARN] Empty evaluation dataframe (0 rows)."

    # 1. 大幅调整权重分布：拉大靶区级差，修正肺癌特征
    priority_map = {
        'PTV': 50, 'GTV': 50, 'CORD': 10, 
        'HEART': 5, 'ESOPHAGUS': 5,
        'LUNGS_NOT_GTV': 8, 'LUNG_L': 2, 'LUNG_R': 2,  # 提升全肺权重
        'CI': 6, 'HI': 6, 
        'SKIN': 1, 'RIND_0': 1, 'RIND_1': 1, 'RIND_2': 1, 'RIND_3': 1, 'RIND_4': 1
    }

    def _get_row_value(row, keys, default=np.nan):
        for k in keys:
            if k in row:
                v = row.get(k)
                if v is None:
                    continue
                if isinstance(v, str) and not v.strip():
                    continue
                if pd.isna(v):
                    continue
                return v
        return default

    def _threshold_num(val):
        """Extract numeric threshold from a value.

        - Accepts numeric columns directly (e.g., Limit/Goal).
        - For legacy text columns (e.g., 'D(95.0%) ≥ 57 Gy'), parses the last number.
        - Avoids parsing metric-only strings like 'D(95.0%)' as 95.
        """
        if val is None:
            return np.nan
        if isinstance(val, (int, float)) and not (isinstance(val, float) and np.isnan(val)):
            return float(val)
        s = str(val)
        if re.search(r'(≤|≥|<=|>=|<|>)', s) or ('GY' in s.upper()):
            return extract_num(s)
        if re.fullmatch(r'\s*[-+]?\d*\.?\d+\s*', s):
            return float(s)
        return np.nan

    def extract_num(val):
        if pd.isna(val) or val == 'nan': return np.nan
        if isinstance(val, (int, float)): return val
        matches = re.findall(r"[-+]?\d*\.\d+|\d+", str(val))
        return float(matches[-1]) if matches else np.nan

    # ==========================================
    # 预检 (Pre-check)：防止靶区剂量坍塌导致的“奖励悖论”
    # ==========================================
    ptv_d95_achieved = None
    ptv_d95_limit = None
    
    for _, row in eval_df.iterrows():
        s = str(_get_row_value(row, ['Struct', 'structure_name'], '')).upper()
        c = str(_get_row_value(row, ['Criterion(Limit)', 'constraint'], '')).upper()
        
        # 寻找主要的靶区覆盖率指标 (D95, D98 等)
        if 'PTV' in s and ('D95' in c or 'D98' in c or 'D(' in c):
            val = _get_row_value(row, ['Achieved Value', 'Plan Value'], np.nan)
            lim = _threshold_num(_get_row_value(row, ['Limit', 'Criterion(Limit)'], np.nan))
            if pd.notna(val) and not np.isinf(val):
                ptv_d95_achieved = val
                ptv_d95_limit = lim if pd.notna(lim) else 60.0
                
                # 如果剂量极低或崩溃（如小于 10 Gy），直接一票否决
                if val < 10.0:
                    return -2000.0, "[FATAL] PTV Dose Collapsed (Achieved < 10 Gy). Optimization Failed."
                break

    total_weighted_score = 0
    total_weight = 0
    details = []

    for _, row in eval_df.iterrows():
        struct = str(_get_row_value(row, ['Struct', 'structure_name'], ''))
        constraint_str = str(_get_row_value(row, ['Criterion(Limit)', 'constraint'], ''))
        
        # ==========================================
        # Bug 4 修复：解析 Struct 为 'nan' 的全局指标
        # ==========================================
        if struct.lower() == 'nan' or not struct.strip():
            if 'CI' in constraint_str.upper():
                struct = 'CI'
            elif 'HI' in constraint_str.upper():
                struct = 'HI'
                
        limit = _threshold_num(_get_row_value(row, ['Limit', 'Criterion(Limit)'], np.nan))
        goal = _threshold_num(_get_row_value(row, ['Goal', 'Criterion(Goal)'], np.nan))
        # achieved = _get_row_value(row, ['Achieved Value', 'Plan Value'], np.nan)
        achieved = _threshold_num(_get_row_value(row, ['Achieved Value', 'Plan Value'], np.nan))
        
        if pd.isna(limit) or pd.isna(goal) or pd.isna(achieved):
            continue
            
        # ==========================================
        # Bug 2 修复：拦截 inf，防止破坏网络权重
        # ==========================================
        if np.isinf(achieved):
            achieved = 9999.0  # 替换为极大值以施加惩罚

        # ==========================================
        # 权重设置 5 修复：无意义的 CI/HI 拦截
        # ==========================================
        if struct in ['CI', 'HI']:
            if ptv_d95_achieved is not None and ptv_d95_limit is not None:
                # 如果靶区覆盖率不到限值的 80%，CI 和 HI 失去讨论意义，不参与打分
                if ptv_d95_achieved < 0.8 * ptv_d95_limit:
                    continue 
            
        # 分配权重
        weight = 1
        search_key = struct.upper() + constraint_str.upper()
        for key, w in priority_map.items():
            if key in search_key:
                weight = w
                break
        
        # 判断越高越好还是越低越好

        # 直接根据数学符号判断高低好坏，绝对精准，不受器官名字变化影响
        if '≥' in constraint_str or '>=' in constraint_str:
            higher_is_better = True
        elif '≤' in constraint_str or '<=' in constraint_str:
            higher_is_better = False
        else:
            # 兜底逻辑：如果文本里没有符号，再退回到关键字判断
            higher_is_better = False
            if 'PTV' in struct.upper() or 'CI' in constraint_str.upper() or 'D(' in constraint_str.upper():
                if 'HI' not in constraint_str.upper() and 'MAX_DOSE' not in constraint_str.upper():
                    higher_is_better = True
        
        metric_score = 0
        
        # ==========================================
        # Bug 3 修复：将除数下限设为 0.1，防止分母极小导致惩罚爆炸
        # ==========================================
        if higher_is_better:
            if achieved >= goal:
                metric_score = 100
            elif achieved >= limit:
                metric_score = 60 + 40 * (achieved - limit) / max(goal - limit, 0.1)
            else:
                penalty_ratio = (limit - achieved) / max(limit, 0.1)
                # Penalize target under-coverage more aggressively to avoid rewarding risky plans.
                if 'PTV' in struct.upper() or 'GTV' in struct.upper():
                    metric_score = -300 * penalty_ratio
                else:
                    metric_score = -100 * penalty_ratio
        else:
            if achieved <= goal:
                metric_score = 100
            elif achieved <= limit:
                metric_score = 60 + 40 * (limit - achieved) / max(limit - goal, 0.1)
            else:
                penalty_ratio = (achieved - limit) / max(limit, 0.1)
                if 'CORD' in struct.upper():
                    metric_score = -500 * (penalty_ratio ** 2)
                else:
                    metric_score = -100 * penalty_ratio
        
        # ==========================================
        # 二重保险：对单项分数进行截断 (Clipping)
        # ==========================================
        metric_score = np.clip(metric_score, -2000, 100)
        
        total_weighted_score += metric_score * weight
        total_weight += weight
        details.append(f"[{struct}|{'Up' if higher_is_better else 'Dn'}]: {metric_score:.1f}")

    final_score = total_weighted_score / total_weight if total_weight > 0 else 0
    return final_score, ", ".join(details)


def generate_TPS_reply(
    self: ConversableAgent,
    messages: Optional[List[Dict]] = None,
    sender: Optional[Agent] = None,
    config: Optional[OpenAIWrapper] = None,
) -> Tuple[bool, Union[str, Dict, None]]:
    """Generate a reply using autogen.oai."""
    if messages is None:
        messages = self._oai_messages[sender]  # sender always is the group manager

    # try cache
    if not sender.traj_file is None:
        dose_out_md = parse_traj(sender.traj_file, sender.iter_count, 'tps')
        if not dose_out_md is None:
            sender.is_skip_physicist = True
            return True, dose_out_md

    sender.is_skip_physicist = False

    # get the optimization parameters from the dosimetrist
    assert messages[-1]['name'] == 'dosimetrist' and messages[-1]['role'] == 'user' 
    response_w_optPara = messages[-1]['content']  # dosimetrist's response with OptPara
    try:
        optPara_md = extract_OptPara(sender.iter_count, response_w_optPara)
        op_json_str = self.j2m.markdown_to_json(optPara_md)
    except Exception as e:
        response = (
            f"Given OptPara Iter-{sender.iter_count}, TPS cannot parse optimization parameters. "
            f"Please regenerate a clean OptPara table with valid numeric fields.\n"
            f"TPS Parsing Error: {e}"
        )
        if not hasattr(sender, 'reward_history'):
            sender.reward_history = []
        sender.reward_history.append(-500)
        sender.optim_time = 0
        return True, response

    adjustment_logs = []
    op_dict = json.loads(op_json_str)

    def _to_float(v):
        try:
            if v is None:
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    def _obj_key(item):
        return (str(item.get('structure_name', '')).upper(), str(item.get('type', '')).lower())

    def _cst_key(item):
        params = item.get('parameters', {}) if isinstance(item, dict) else {}
        return (str(params.get('structure_name', '')).upper(), str(item.get('type', '')).lower())

    def _build_occurrence_map(items, key_fn):
        key_counts = {}
        mapped = {}
        for item in items:
            base = key_fn(item)
            idx = key_counts.get(base, 0)
            key_counts[base] = idx + 1
            mapped[(base[0], base[1], idx)] = item
        return mapped

    def _clamp_field(curr_obj, prev_obj, field_path, desc):
        curr_ref = curr_obj
        prev_ref = prev_obj
        for key in field_path[:-1]:
            if not isinstance(curr_ref, dict) or not isinstance(prev_ref, dict):
                return
            curr_ref = curr_ref.get(key, {})
            prev_ref = prev_ref.get(key, {})
        leaf = field_path[-1]
        if not isinstance(curr_ref, dict) or not isinstance(prev_ref, dict):
            return
        curr_v = _to_float(curr_ref.get(leaf))
        prev_v = _to_float(prev_ref.get(leaf))
        if curr_v is None or prev_v is None or prev_v == 0:
            return
        low = min(prev_v * 0.95, prev_v * 1.05)
        high = max(prev_v * 0.95, prev_v * 1.05)
        new_v = min(max(curr_v, low), high)
        if abs(new_v - curr_v) > 1e-9:
            curr_ref[leaf] = float(new_v)
            adjustment_logs.append(
                f"Step clamp ({desc}): {curr_v:.4g} -> {new_v:.4g} (prev {prev_v:.4g}, +/-5%)."
            )

    # Parse previous dosimetrist OptPara for high-score step-size clamp
    prev_op_dict = None
    prev_msg = next(
        (m for m in reversed(messages[:-1]) if m.get('role') == 'user' and m.get('name') == 'dosimetrist'),
        None,
    )
    if prev_msg is not None:
        try:
            prev_opt_md = extract_OptPara('prev', prev_msg.get('content', ''))
            prev_op_dict = json.loads(self.j2m.markdown_to_json(prev_opt_md))
        except Exception:
            prev_op_dict = None

    prev_reward = sender.reward_history[-1] if hasattr(sender, 'reward_history') and sender.reward_history else None
    if prev_reward is not None and prev_reward >= 99 and prev_op_dict is not None:
        prev_obj_map = _build_occurrence_map(prev_op_dict.get('objective_functions', []), _obj_key)
        cur_obj_counts = {}
        for obj in op_dict.get('objective_functions', []):
            base = _obj_key(obj)
            idx = cur_obj_counts.get(base, 0)
            cur_obj_counts[base] = idx + 1
            prev_obj = prev_obj_map.get((base[0], base[1], idx))
            if prev_obj is None:
                continue
            _clamp_field(obj, prev_obj, ('weight',), f"objective weight {base[0]} {base[1]}")
            _clamp_field(obj, prev_obj, ('dose_gy',), f"objective dose_gy {base[0]} {base[1]}")

        prev_cst_map = _build_occurrence_map(prev_op_dict.get('constraints', []), _cst_key)
        cur_cst_counts = {}
        for cst in op_dict.get('constraints', []):
            base = _cst_key(cst)
            idx = cur_cst_counts.get(base, 0)
            cur_cst_counts[base] = idx + 1
            prev_cst = prev_cst_map.get((base[0], base[1], idx))
            if prev_cst is None:
                continue
            _clamp_field(cst, prev_cst, ('constraints', 'limit_dose_gy'), f"constraint limit_dose_gy {base[0]} {base[1]}")
            _clamp_field(cst, prev_cst, ('constraints', 'limit_volume_perc'), f"constraint limit_volume_perc {base[0]} {base[1]}")
            _clamp_field(cst, prev_cst, ('parameters', 'dose_gy'), f"constraint parameters.dose_gy {base[0]} {base[1]}")

    # Always sanitize risky DVH constraints without dropping key-structure optimization targets.
    safe_constraints = []
    softened_risky = 0
    for c in op_dict.get('constraints', []):
        c_type = str(c.get('type', '')).lower()
        struct = str(c.get('parameters', {}).get('structure_name', '')).upper()
        dose_gy = _to_float(c.get('parameters', {}).get('dose_gy'))
        vol_lim = _to_float(c.get('constraints', {}).get('limit_volume_perc'))
        risky_struct = struct == 'PTV' or struct == 'LUNGS_NOT_GTV' or struct.startswith('RIND_')
        if c_type == 'dose_volume_v' and risky_struct and dose_gy is not None and vol_lim is not None:
            if dose_gy >= 60 and vol_lim <= 1.5:
                # Keep optimization for key structures, but relax overly aggressive DVH settings.
                c.setdefault('constraints', {})['limit_volume_perc'] = 2.0
                softened_risky += 1
        safe_constraints.append(c)
    if softened_risky > 0:
        adjustment_logs.append(
            f"Softened {softened_risky} risky dose_volume_V constraints by setting limit_volume_perc to 2.0 (PTV/LUNGS_NOT_GTV/RIND_* with dose_gy>=60 and limit_volume_perc<=1.5)."
        )
    op_dict['constraints'] = safe_constraints

    # Cap PTV quadratic-underdose weight to avoid numeric inflation
    for obj in op_dict.get('objective_functions', []):
        if str(obj.get('structure_name', '')).upper() == 'PTV' and str(obj.get('type', '')).lower() == 'quadratic-underdose':
            w = _to_float(obj.get('weight'))
            if w is not None and w > 2500000:
                obj['weight'] = 2500000.0
                adjustment_logs.append(f"Capped PTV quadratic-underdose weight from {w:.4g} to 2500000.")

    # Filter out dose_volume_V constraints in the first iteration to prevent solver timeout
    if sender.iter_count <= 1:
        before = len(op_dict.get('constraints', []))
        op_dict["constraints"] = [c for c in op_dict.get("constraints", []) if c.get("type") != "dose_volume_V"]
        removed = before - len(op_dict.get('constraints', []))
        adjustment_logs.append(f"Iter-1 safety rule: removed all dose_volume_V constraints ({removed} removed).")

    op_json_str = json.dumps(op_dict)

    try:
        solution, dose_out_md, dose_out_df, elapsed_time = self.imrt.do_optim(op_json_str)
    except Exception as e:
        response = (
            f"Given OptPara Iter-{sender.iter_count}, TPS optimization failed due to a runtime error. "
            f"Please roll back to the last feasible OptPara and make conservative adjustments.\n"
            f"TPS Runtime Error: {e}"
        )
        if adjustment_logs:
            response += "\n\n### Parameter Sanitization Applied:"
            for log_line in adjustment_logs:
                response += f"\n- {log_line}"
        if not hasattr(sender, 'reward_history'):
            sender.reward_history = []
        sender.reward_history.append(-1000)
        sender.optim_time = getattr(sender, 'optim_time_limit', 1200)
        return True, response
    sender.dose_out_df = dose_out_df
    sender.optim_time = elapsed_time

    # calculate RL reward
    current_reward, reward_details = calculate_rl_reward(dose_out_df)
    
    # store in manager for trajectory tracking and stop criteria
    if not hasattr(sender, 'reward_history'):
        sender.reward_history = []
    
    prev_reward = sender.reward_history[-1] if sender.reward_history else current_reward
    reward_diff = current_reward - prev_reward
    sender.reward_history.append(current_reward)

    # save solution to pickle file
    traj_folder = f'{sender.traj_folder}/{sender.pid}'
    if not os.path.exists(traj_folder):
        os.makedirs(traj_folder)
    with open(f'{traj_folder}/solution_x_{sender.iter_count}.pkl', 'wb') as f:
        pickle.dump(solution['optimal_intensity'], f)

    # handle the optimization complexity
    response = f"Given OptPara Iter-{sender.iter_count}, the dosimetric outcomes produced by TPS are:\n {dose_out_md}"
    
    # Append Reward Info
    response += f"\n\n### RL Reward Analysis (Iter-{sender.iter_count}):"
    response += f"\n- **Current Total Score**: {current_reward:.2f}"
    response += f"\n- **Score Change**: {reward_diff:+.2f} ({'Improved' if reward_diff > 0 else 'Degraded' if reward_diff < 0 else 'No Change'})"
    response += f"\n- **Score Details**: {reward_details}"

    if adjustment_logs:
        response += "\n\n### Parameter Sanitization Applied:"
        for log_line in adjustment_logs:
            response += f"\n- {log_line}"

    # Precise PTV D95 detection
    try:
        # Regex to match PTV-related structures and D(95...
        struct_col = 'Struct' if 'Struct' in dose_out_df.columns else ('structure_name' if 'structure_name' in dose_out_df.columns else None)
        crit_col = 'Criterion(Limit)' if 'Criterion(Limit)' in dose_out_df.columns else ('constraint' if 'constraint' in dose_out_df.columns else None)
        achieved_col = 'Achieved Value' if 'Achieved Value' in dose_out_df.columns else ('Plan Value' if 'Plan Value' in dose_out_df.columns else None)
        if struct_col is None or crit_col is None or achieved_col is None:
            raise KeyError('Required columns not found for D95 detection')
        d95_mask = (
            dose_out_df[struct_col].astype(str).str.contains('PTV', case=False, na=False)
            & dose_out_df[crit_col].astype(str).str.contains(r'D\(95', regex=True, na=False)
        )
        d95 = float(dose_out_df.loc[d95_mask, achieved_col].values[0])
    except (IndexError, KeyError, ValueError):
        # Fallback if D95 is not found for some reason
        d95 = 60 

    if d95 < 50:
        response += "\n\nCRITICAL TPS ERROR: The current constraints are physically infeasible, resulting in an optimization collapse (PTV D95 < 50 Gy). The optimization problem is mathematically unsolvable with these strict constraints. You MUST ROLLBACK your parameters immediately. Do not attempt to tweak the current broken parameters. You must output the OptPara table from the precise LAST SUCCESSFUL iteration exactly as it was, and abandon your recent extreme constraints."
        # Keep collapse penalty consistent with fatal scoring branch.
        sender.reward_history[-1] = -2000
    elif elapsed_time > getattr(sender, 'optim_time_limit', 1200) - 1:
        response += "\nTPS Warning: Optimization cannot solve the optimization problem within the time limit, resulting in a suboptimal solution. This may be due to the complexity of dose_volume_V constraints. You may consider simplifying OptPara to reduce the optimization complexity."
        # Penalty for timeout to push agent towards simpler constraints
        sender.reward_history[-1] -= 200
        response += f"\n- **Time Penalty**: -200 applied to current score due to optimization timeout."

    # Hard-stop: terminate the group chat when a perfect score is achieved.
    try:
        final_reward = float(sender.reward_history[-1])
    except Exception:
        final_reward = None
    if final_reward is not None and final_reward >= 100.0:
        response += "\n\nTERMINATE"

    return True, response

def generate_default_humanSupervisor_reply(
    self: ConversableAgent,
    messages: Optional[List[Dict]] = None,
    sender: Optional[Agent] = None,
    config: Optional[OpenAIWrapper] = None,
) -> Tuple[bool, Union[str, Dict, None]]:
    client = self.client if config is None else config
    if messages is None:
        messages = self._oai_messages[sender]  # sender always is the group manager
    assert self._name == 'human_supervisor', 'Only human_supervisor agent can use this function'

    extracted_response = 'No comments.'

    return (False, None) if extracted_response is None else (True, extracted_response)

def test_parse_traj():
    traj_file_path = './debug/Lung_Patient_42/optim_trajectories_full.md'
    iter_idx = 3 
    role = 'dosimetric'
    print(parse_traj(traj_file_path, iter_idx, role))
    role = 'TPS'
    print(parse_traj(traj_file_path, iter_idx, role))

def test_dosimetrist_extract_OptPara():
    response = textwrap.dedent("""\
### Updated OptPara Iter-21
```
| ROI Name      | Objective Type       | Target Gy | % Volume | Weight   |
|:--------------|:---------------------|:----------|:---------|:---------|
| PTV           | quadratic-overdose   | 60        | NA       | 15000    |
| PTV           | quadratic-underdose  | 60        | NA       | 620000   |
| PTV           | quadratic-underdose  | 60.5      | NA       | 460000   |
| PTV           | dose_volume_V        | 60        | 99       | NA       |
| PTV           | max_dose             | 68        | NA       | NA       |
| GTV           | max_dose             | 68        | NA       | NA       |
| CORD          | linear-overdose      | 40        | NA       | 1600     |
| CORD          | quadratic            | NA        | NA       | 15       |
| CORD          | max_dose             | 47        | NA       | NA       |
| ESOPHAGUS     | quadratic            | NA        | NA       | 20       |
| ESOPHAGUS     | max_dose             | 60        | NA       | NA       |
| ESOPHAGUS     | mean_dose            | 34        | NA       | NA       |
| HEART         | quadratic            | NA        | NA       | 20       |
| HEART         | max_dose             | 60        | NA       | NA       |
| HEART         | mean_dose            | 25        | NA       | NA       |
| HEART         | dose_volume_V        | 30        | 40       | NA       |
| LUNGS_NOT_GTV | quadratic            | NA        | NA       | 20       |
| LUNGS_NOT_GTV | max_dose             | 63        | NA       | NA       |
| LUNGS_NOT_GTV | mean_dose            | 16        | NA       | NA       |
| LUNGS_NOT_GTV | dose_volume_V        | 20        | 30       | NA       |
| LUNG_L        | quadratic            | NA        | NA       | 10       |
| LUNG_L        | max_dose             | 63        | NA       | NA       |
| LUNG_R        | quadratic            | NA        | NA       | 10       |
| LUNG_R        | max_dose             | 63        | NA       | NA       |
| RIND_0        | quadratic            | NA        | NA       | 12       |
| RIND_0        | max_dose             | 66        | NA       | NA       |
| RIND_1        | quadratic            | NA        | NA       | 12       |
| RIND_1        | max_dose             | 63        | NA       | NA       |
| RIND_2        | quadratic            | NA        | NA       | 7        |
| RIND_2        | max_dose             | 54        | NA       | NA       |
| RIND_3        | quadratic            | NA        | NA       | 7        |
| RIND_3        | max_dose             | 51        | NA       | NA       |
| RIND_4        | quadratic            | NA        | NA       | 7        |
| RIND_4        | max_dose             | 45        | NA       | NA       |
| SKIN          | max_dose             | 60        | NA       | NA       |
| NA            | smoothness-quadratic | NA        | NA       | 1800     |
```
""")
    dosimetrist_post_process(None, 48, [], response, None)    

if __name__ == '__main__':
    test_dosimetrist_extract_OptPara()
