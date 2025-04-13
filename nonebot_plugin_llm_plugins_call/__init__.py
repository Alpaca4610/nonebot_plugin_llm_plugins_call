import asyncio
import copy
import json
import nonebot
import re
import yaml

from openai import AsyncOpenAI
from .config import Config, ConfigError
from typing import Any, Dict
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.log import logger
from nonebot.rule import Rule,to_me
from nonebot.plugin import Plugin,PluginMetadata
from nonebot import on_command, require
from nonebot.adapters.onebot.v11 import (
    Message,
    MessageEvent,
    GroupMessageEvent,
    Bot,
    GROUP
)
from nonebot import get_driver, get_plugin_config

require("nonebot_plugin_saa")
from nonebot_plugin_saa import Text

__plugin_meta__ = PluginMetadata(
    name="LLM调用nonebot插件",
    description="通过LLM结合语境调用已安装的nonebot插件，实现更拟人和自然机器人聊天风格",
    usage="""
    @机器人即可
    """,
    config=Config,
    extra={},
    type="application",
    homepage="https://github.com/Alpaca4610/nonebot_plugin_llm_plugins_call",
    supported_adapters={"~onebot.v11"},
)

driver = get_driver()
config = nonebot.get_driver().config
prefix = list(config.command_start)[0]
tools = []

plugin_config = get_plugin_config(Config)

if not plugin_config.plugins_call_key:
    raise ConfigError("请配置plugins_call大模型使用的KEY")
if plugin_config.plugins_call_api_url:
    client = AsyncOpenAI(
        api_key=plugin_config.plugins_call_key, base_url=plugin_config.plugins_call_api_url
    )
else:
    client = AsyncOpenAI(api_key=plugin_config.plugins_call_key)

model_id = plugin_config.plugins_call_llm

# 初始化插件信息字典
new_plugin_info: Dict[str, Any] = {}

def load_plugin_data(json_path: str) -> Dict[str, Any]:
    global new_plugin_info
    try:
        if not json_path:
            new_plugin_info =  {}
            
        with open(json_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            new_plugin_info =  {item["module_name"]: item for item in data if "module_name" in item}
            
    except Exception as e:
        new_plugin_info =  {}

    

default_blacklist = ["nonebot_plugin_saa", "nonebot_plugin_apscheduler", "nonebot_plugin_localstore", "nonebot_plugin_htmlrender",
             "nonebot_plugin_tortoise_orm", "nonebot_plugin_alconna.uniseg", "nonebot_plugin_cesaa", "nonebot_plugin_session_saa", "nonebot_plugin_orm","nonebot_plugin_llm_plugins_call"]
blacklist = default_blacklist + plugin_config.plugins_call_blacklist

def modify_string(input_str, new_string):
    pattern = r'(\[CQ:at,qq=\d+\])\s*.*'
    return re.sub(pattern, lambda m: f'{m.group(1)} {new_string}', input_str)


def create_tool_entry(plugin_id: str, description: str, command_desc: str) -> dict:
    return {
        'type': 'function',
        'function': {
            'name': plugin_id,
            'description': description,
            'parameters': {
                'type': 'object',
                'properties': {
                    'command': {
                        'type': 'string',
                        'description': command_desc
                    }
                },
                'required': ['command']
            }
        }
    }


def generate_tools_json(plugin_set, blacklist=None):
    if blacklist is None:
        blacklist = set()

    tools = []

    load_plugin_data(plugin_config.plugins_call_metadata_file)
    
    
    for plugin in plugin_set:
        if plugin.module_name in blacklist:
            continue

        new_meta = new_plugin_info.get(plugin.module_name, {})
        matcher = getattr(plugin, 'matcher', [])
        if not matcher:
            continue

        description = new_meta.get("description") or getattr(
            getattr(plugin, 'metadata', None), 'description', None
        )
        if not description:
            continue


        tool = create_tool_entry(
            plugin.module_name,
            description,
            command_desc=""
        )
        tools.append(tool)
        logger.info(f"成功读取插件 {plugin.module_name} 的元数据")

    return tools


@driver.on_startup
async def do_something():
    plugins: set[Plugin] = nonebot.get_loaded_plugins()
    global tools
    tools = generate_tools_json(plugins)



async def to_me_rule(event: GroupMessageEvent) -> bool:
    if not hasattr(event, "message_seq"):
        return True
    return event.message_seq is not None


to_me_reply = on_command(
    "",
    rule= Rule(to_me_rule) & to_me(),
    priority=999,
    block=True,
    permission=GROUP
)


@to_me_reply.handle()
async def _(bot: Bot, event: MessageEvent, msg: Message = CommandArg()):
    content = msg.extract_plain_text()

    messages = [{'role': 'user', 'content': f"请你分析用户自然的语言，结合提供给你的tools(插件)列表，分析自然语言中是否含有插件调用需求，决定是否调用和应该调用哪个插件。若插件列表中没有符合用户当前需求的插件，或者用户在正常闲聊，则不触发插件，给用户返回提示或者陪他聊天。若插件列表里有的功能和你能做的事情重合，优先选择插件列表里面的功能。请注意,你的回复内容不要包含任何选择插件的思考推理过程透露你用于选择插件的用途\n"}, {
        'role': 'user', 'content': content}]

    response = await client.chat.completions.create(
        model=model_id,
        messages=messages,
        temperature=0.01,
        top_p=0.95,
        stream=False,
        tools=tools
    )

    # logger.info(response)
    if response.choices:
        message = response.choices[0].message
        if hasattr(message, 'tool_calls') and message.tool_calls:
            tool_call = message.tool_calls[0]

            func1_name = tool_call.function.name
            logger.info("LLM选择使用插件：" + func1_name)

            select_plugin: Plugin | None = nonebot.get_plugin_by_module_name(
                func1_name)


            new_meta = new_plugin_info.get(func1_name, {})
    

            description = new_meta.get("description") or getattr(
                getattr(select_plugin, 'metadata', None), 'description', ""
            )
            usage = new_meta.get("usage", "") or getattr(
                getattr(select_plugin, 'metadata', None), 'usage', ""
            )

            select_plugin_matcher = getattr(select_plugin, 'matcher', [])
            rules = []
            count = 0
            for m in select_plugin_matcher:
                rule = getattr(m, 'rule', None)
                is_tome = ""
                call_ = ""
                prefix_prompt = ""
                is_command = False
                if rule:
                    for checker in rule.checkers:
                        call = checker.call
                        if str(call) == "ToMe()":
                            is_tome = "，需要@触发，请在最终命令最前面加上@"
                            continue
                        if str(call).startswith("Command") and prefix != "":
                            is_command = True    
                        call_ += str(call)
                    if is_command and is_tome != "":
                        prefix_prompt = f"还需要前缀触发，在@后面加上{prefix}"
                    elif is_command and is_tome == "":
                        prefix_prompt = f"需要前缀触发，在命令前加上{prefix}"
                        
                    rule_str_ = f"命令{count}:触发规则:{call_}{is_tome}{prefix_prompt}"
                    rules.append(rule_str_)
                    count += 1

            if rules:
                rule_str = "\n".join(rules)
                full_desc = f"{description}\n功能描述：{usage}".strip()
                select_tools = [create_tool_entry(
                    func1_name, 
                    full_desc,
                    command_desc=f"触发规则：\n{rule_str}"
                )]

            # if prefix != "":
            #     prefix_prompt = f"注意，如果触发规则类型为Command()，你需要在命令的最前面（如果命令需要@触发，则在@的后面）加上前缀{prefix}"
            # else:
            #     prefix_prompt = ""
                
            messages_ = [{'role': 'user', 'content': f"""分析用户的自然语言，结合提供的tool(插件)，从自然语言中提取用于触发该插件的参数，结合参数构造纯文本触发命令。插件的功能描述和命令触发规则已经提供，需要你分析功能描述，结合命令触发规则构造规则出带参数的能够精准触发该插件的命令。一个插件可能有对应不同功能的多条命令匹配规则，请你选择最合适的。参考功能介绍，结合用户自然语言中暗含的参数来构造命令（提取的参数最贴近用户的自然语言中的语义），并且最终构造的命令要符合触发规则。需要特别注意前缀问题，插件的功能描述介绍了该插件的功能列表，列表中的命令可能带有一些前缀。但是在实际使用中，不同用户的前缀可能设定不一致，有的用户可能删除了命令前缀或者换成别的前缀(可以为空)，功能描述仅供判断选择哪个功能进行触发和参考构造命令所需要的参数，选择好功能后必须按照触发规则来构造最终的触发命令文本。另外你必须使用tools_call功能，在调用的工具的参数里面回复我，不能直接回复我。\n 用户的自然语言为：{content}"""}]

            
            response_ = await client.chat.completions.create(
                model=model_id,
                messages=messages_,
                temperature=0.01,
                top_p=0.95,
                stream=False,
                tools=select_tools
            )

            # logger.info(response_)
            if hasattr(message, 'tool_calls') and message.tool_calls:
                func1_args = response_.choices[0].message.tool_calls[0].function.arguments
                data = json.loads(func1_args)
                func1_args = data["command"]
                logger.info("构造的命令为" + func1_args)

                # logger.info("机器人回复"+ response.choices[0].message.content)

                new_event = copy.deepcopy(event)

                if func1_args and func1_args.startswith('@'):
                    func1_args = func1_args[1:]
                    new_event.to_me = True
                else:
                    new_event.to_me = False

                # logger.info(event.get_message)

                # new_event.message_id = None
                new_event.message_seq = None
                new_event.real_seq = None

                for seg in new_event.message:
                    if seg.is_text():
                        seg.data["text"] = func1_args
                        break
                new_event.original_message[1].data["text"] = " " + func1_args
                new_event.raw_message = modify_string(
                    new_event.raw_message, func1_args)

                ## info(new_event.get_message)
                await Text(" plugin_call调用nonebot插件：" + str(select_plugin.name)).send(at_sender=True)
                asyncio.create_task(bot.handle_event(new_event))
                return
            else:
                return
        else:
            logger.info("No tool_calls found in response")
            await Text(str(response.choices[0].message.content)).finish(at_sender=True)
    else:
        logger.error("No choices in API response")
        return
