from pokeldn.plugins import Field, Tool


def register(api):
    fields = (
        Field(
            "--code", "联机密码", "text", default="00000000",
            help="输入与 Switch 本地交换界面相同的 8 位数字密码。",
        ),
        Field(
            "--channel", "无线信道", "number", default="6", bounds=(1, 13),
            help="默认 6；一般无需修改。",
        ),
        Field(
            "--seconds", "最长等待时间（秒）", "number", default="1800", bounds=(120, 3600),
            help="连接等待上限；一般保持默认。",
        ),
    )
    api.add_tool(
        "pla",
        Tool(
            "pla-mmo-seed-assistant",
            "阿尔宙斯 MMO Seed 助手",
            "pla_mmo_seed.py",
            "采集四只宝可梦的交换确认页信息，自动反推《宝可梦传说 阿尔宙斯》群体 Seed，并保存结果。",
            (
                "在《宝可梦传说 阿尔宙斯》中前往祝庆村，找到交换 NPC，选择本地交换。",
                "在 Switch 与本插件中输入相同的 8 位联机密码，等待连接成功。",
                "在宝可梦箱子中选中一只最初捕获的宝可梦，进入“确定要进行宝可梦的交换吗？”确认页面。看到收录信息后，按 B 返回箱子。",
                "不要确认真正交换。选择下一只宝可梦，并重复进入确认页面，直到收录进度达到 4/4。",
                "第四只记录保存后，插件会自动结束联机并计算 Seed。计算成功后，结果会显示在页面，并保存到 latest_seed.txt。",
                "如果提示缺少计算程序，请将 EtumrepMMO 官方 Windows x64 发布包中的全部文件解压到插件数据目录的 solver 文件夹。已保存的宝可梦记录无需重新采集。",
            ),
            fields=fields,
            fixed=(
                "--mode", "collect",
                "--keys", "{keys}",
                "--trainer", "{ot}|{tid}|{sid}|{language}|{gender}",
                "--stamp", "{stamp}",
            ),
            doc="",
        ),
    )
