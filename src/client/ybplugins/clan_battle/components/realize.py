import math
import os
import json
import peewee
import base64
import random
import string
import asyncio
import logging
from io import BytesIO
from PIL import Image, ImageFont, ImageDraw
from typing import Any, Dict, List, Optional, Union, Tuple

from ..typing import ClanBattleReport, Groupid, Pcr_date, QQid
from ...web_util import async_cached_func
from ..util import atqq, pcr_datetime, pcr_timestamp, timed_cached_func

from ...ybdata import Clan_challenge, Clan_group, Clan_member, User, Clan_group_backups
from ..exception import GroupError, GroupNotExist, InputError, UserError, UserNotInGroup

_logger = logging.getLogger(__name__)
FILE_PATH = os.path.dirname(__file__)

def safe_load_json(text, back = None):
	return text and json.loads(text) or back
def text_2_pic(self, text:string, weight:int, height:int, bg_color:Tuple, text_color:string, font_size:int, text_offset:Tuple):
	im = Image.new("RGB", (weight, height), bg_color)
	dr = ImageDraw.Draw(im)
	FONTS_PATH = os.path.join(FILE_PATH,'fonts')
	FONTS = os.path.join(FONTS_PATH,'msyh.ttf')
	font = ImageFont.truetype(FONTS, font_size)
	dr.text(text_offset, text, font=font, fill=text_color)
	bio = BytesIO()
	im.save(bio, format='PNG')
	base64_str = 'base64://' + base64.b64encode(bio.getvalue()).decode()
	return f"[CQ:image,file={base64_str}]"


#阶段周目
def _level_by_cycle(self, cycle, game_server=None):
	level = 0
	for lv in self.setting['level_by_cycle'][game_server]:
		if cycle >= lv[0] and cycle <= lv[1] : return level
		level += 1
	return level

#通过qq号获取名字
@timed_cached_func(128, 3600, ignore_self=True)
def _get_nickname_by_qqid(self, qqid) -> Union[str, None]:
	user = User.get_or_create(qqid=qqid)[0]
	if user.nickname is None:
		asyncio.ensure_future(self._update_user_nickname_async(
			qqid = qqid, group_id = None))
	return user.nickname or str(qqid)

#获取上一个出刀记录
def _get_group_previous_challenge(self, group: Clan_group):
	Clan_challenge_alias = Clan_challenge.alias()
	query = Clan_challenge.select().where(
		Clan_challenge.cid == Clan_challenge_alias.select(
			peewee.fn.MAX(Clan_challenge_alias.cid)
		).where(
			Clan_challenge_alias.gid == group.group_id,
			Clan_challenge_alias.bid == group.battle_id,
		)
	)
	try : return query.get()
	except peewee.DoesNotExist:return None

#更新群列表
async def _update_group_list_async(self):
	try : group_list = await self.api.get_group_list()
	except Exception as e:
		_logger.exception('获取群列表错误'+str(e))
		return False

	for group_info in group_list:
		group = Clan_group.get_or_none(group_id=group_info['group_id'],)
		if group is None : continue
		group.group_name = group_info['group_name']
		group.save()
	return True

#获取群成员列表
@async_cached_func(16)
async def _fetch_member_list_async(self, group_id):
	try:
		group_member_list = await self.api.get_group_member_list(group_id=group_id)
	except Exception as e:
		_logger.exception('获取群成员列表错误' + str(type(e)) + str(e))
		asyncio.ensure_future(self.api.send_group_msg(
			group_id = group_id, message = '获取群成员错误，这可能是缓存问题，请重启go-cqhttp后再试'))
		return []
	return group_member_list

#更新所有群成员
async def _update_all_group_members_async(self, group_id):
	group_member_list = await self._fetch_member_list_async(group_id)
	for member in group_member_list:
		user = User.get_or_create(qqid=member['user_id'])[0]
		membership = Clan_member.get_or_create(group_id = group_id, qqid = member['user_id'])[0]
		user.nickname = member.get('card') or member['nickname']
		user.clan_group_id = group_id
		if user.authority_group >= 10:
			user.authority_group = (100 if member['role'] == 'member' else 10)
			membership.role = user.authority_group
		user.save()
		membership.save()

	# refresh member list
	self.get_member_list(group_id, nocache = True)

#更新成员名字
async def _update_user_nickname_async(self, qqid, group_id = None):
	try:
		user = User.get_or_create(qqid=qqid)[0]
		if group_id is None:
			userinfo = await self.api.get_stranger_info(user_id=qqid)
			user.nickname = userinfo['nickname']
		else:
			userinfo = await self.api.get_group_member_info(group_id=group_id, user_id=qqid)
			user.nickname = userinfo['card'] or userinfo['nickname']
		user.save()

		# refresh
		if user.nickname is not None : self._get_nickname_by_qqid(qqid, nocache=True)
	except Exception as e : _logger.exception(e)

#获取boss当前数据
def _boss_data_dict(self, group: Clan_group) -> Dict[str, Any]:
	cycle = group.boss_cycle
	now_health = safe_load_json(group.now_cycle_boss_health, {})
	next_health = safe_load_json(group.next_cycle_boss_health, {})
	challenging_member_list = safe_load_json(group.challenging_member_list, {})

	back_data = {}
	for i in range(5):
		str_boss_num = str(i + 1)
		num_boss_num = i + 1
		next_flag = now_health[str_boss_num] == 0
		level = self._level_by_cycle(cycle, group.game_server)
		icon_id = self.setting['boss_id'][group.game_server][i]
		back_data[num_boss_num] = {
			'is_next': next_flag,
			'cycle': next_flag and cycle+1 or cycle,
			'health': 0 if now_health[str_boss_num] == 0 and not check_next_boss(self, group.group_id, str_boss_num)
						else next_flag and next_health[str_boss_num] or now_health[str_boss_num],
			'full_health': self.bossinfo[group.game_server][level][i],
			'challenger': str_boss_num in challenging_member_list and challenging_member_list[str_boss_num] or 0,
			'icon_id': icon_id,
			'name': self.boss_id_name[str_boss_num][icon_id]
		}
	return back_data




#创建公会
def create_group(self, group_id: Groupid, game_server, group_name=None) -> None:
	"""
	Args:
		group_id: QQ群号
		group_name: QQ群名，用作公会名
		game_server: 服务器名("jp" "tw" "cn" "kr")
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None:
		now_cycle_boss_health = {}
		level = self._level_by_cycle(1, game_server)
		for boss_num, health in enumerate(self.bossinfo[game_server][level]):
			now_cycle_boss_health[boss_num+1] = health
		next_cycle_boss_health = {}
		level = self._level_by_cycle(2, game_server)
		for boss_num, health in enumerate(self.bossinfo[game_server][level]):
			next_cycle_boss_health[boss_num+1] = health
		group = Clan_group.create(
			group_id = group_id,
			group_name = group_name,
			game_server = game_server,
			now_cycle_boss_health = json.dumps(now_cycle_boss_health),
			next_cycle_boss_health = json.dumps(next_cycle_boss_health),
		)
	elif group.deleted:
		group.deleted = False
		group.game_server = game_server
		group.save()
	else : raise GroupError('群已经存在')
	self._boss_status[group_id] = asyncio.get_event_loop().create_future()

	# refresh group list
	asyncio.ensure_future(self._update_group_list_async())

#加入公会
async def bind_group(self, group_id:Groupid, qqid:QQid, nickname:str):
	"""
	Args:
		group_id: QQ群号
		qqid: 加入公会的成员QQ号
		nickname: 用来显示的名字
	"""
	user = User.get_or_create(qqid=qqid)[0]
	user.clan_group_id = group_id
	user.nickname = nickname
	user.deleted = False
	try:
		groupmember = await self.api.get_group_member_info(group_id = group_id, user_id = qqid)
		role = 100 if groupmember['role'] == 'member' else 10
	except Exception as e:
		_logger.exception(e)
		role = 100
	membership = Clan_member.get_or_create(
		group_id = group_id,
		qqid = qqid,
		defaults = {'role': role})[0]
	user.save()

	# refresh
	self.get_member_list(group_id, nocache=True)
	if nickname is None:
		asyncio.ensure_future(self._update_user_nickname_async(qqid = qqid, group_id = group_id))
	return membership

#删除成员
def drop_member(self, group_id: Groupid, member_list: List[QQid]):
	"""
	删除公会里的成员（一般在面板里操作，可同时删除多个）

	在调用此函数之前，要先检查操作者权限。
	Args:
		group_id: QQ群号
		member_list: 要被删除的成员QQ号列表
	"""
	delete_count = Clan_member.delete().where(
		Clan_member.group_id == group_id,
		Clan_member.qqid.in_(member_list)
	).execute()

	for user_id in member_list:
		user = User.get_or_none(qqid=user_id)
		if user is not None:
			user.clan_group_id = None
			user.save()

	# refresh member list
	self.get_member_list(group_id, nocache=True)
	return delete_count

#修改boss状态
def modify(self, group_id: Groupid, cycle=None, bossData=None):
	"""
	在调用此函数之前，要先检查操作者权限。

	Args:
		group_id: group id
		cycle: 要改到第几周目
		bossData: 结构和 _boss_data_dict() 获取的数据一致
	"""
	if cycle and cycle < 1:
		raise InputError('周目数不能为负')

	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist

	next_cycle_level = self._level_by_cycle(cycle and cycle+1 or group.boss_cycle+1, group.game_server)
	now_health = safe_load_json(group.now_cycle_boss_health, {})
	next_health = safe_load_json(group.next_cycle_boss_health, {})

	for boss_num, data in bossData.items():
		next_cycle_full_boss_health = self.setting['boss'][group.game_server][next_cycle_level][int(boss_num)-1]
		if data["is_next"]:
			now_health[boss_num] = 0
			next_health[boss_num] = data["health"]
		else:
			now_health[boss_num] = data["health"]
			next_health[boss_num] = next_cycle_full_boss_health
	
	group.now_cycle_boss_health = json.dumps(now_health)
	group.next_cycle_boss_health = json.dumps(next_health)
	group.boss_cycle = cycle

	group.save()

	msg = 'boss状态已修改'
	self._boss_status[group_id].set_result((self._boss_data_dict(group), group.boss_cycle, msg))
	self._boss_status[group_id] = asyncio.get_event_loop().create_future()
	return msg

#修改服务器
def change_game_server(self, group_id: Groupid, game_server):
	"""
	在调用此函数之前，要先检查操作者权限。

	Args:
		group_id: QQ群号
		game_server: 服务器名("jp" "tw" "cn" "kr")
	"""
	if game_server not in ("jp", "tw", "cn", "kr"):
		raise InputError(f'不存在{game_server}游戏服务器')
	group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	group.game_server = game_server
	group.save()

#获取当期会战数据记录档案的编号
def get_data_slot_record_count(self, group_id: Groupid):
	"""
	创建新档并重置boss状态
	挑战数据应进行备份和确认
	在调用此函数之前，要先检查操作者权限。

	Args:
		group_id: QQ群号
	"""
	group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	counts = []
	for c in Clan_challenge.select(
		Clan_challenge.bid,
		peewee.fn.COUNT(Clan_challenge.cid).alias('record_count'),
	).where(Clan_challenge.gid == group_id).group_by(Clan_challenge.bid,):
		counts.append({'battle_id': c.bid, 'record_count': c.record_count})
	return counts

#清空会战数据记录档案
def clear_data_slot(self, group_id: Groupid, battle_id: Optional[int] = None):
	"""
	清空选择的档案并重置boss状态
	挑战数据应进行备份和确认
	在调用此函数之前，要先检查操作者权限。

	Args:
		group_id: QQ群号
		battle_id: 选择的档案号
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None:
		raise GroupNotExist

	now_cycle_boss_health = {}
	level = self._level_by_cycle(1, group.game_server)
	for boss_num, health in enumerate(self.bossinfo[group.game_server][level]):
		now_cycle_boss_health[boss_num+1] = health
	next_cycle_boss_health = {}
	level = self._level_by_cycle(2, group.game_server)
	for boss_num, health in enumerate(self.bossinfo[group.game_server][level]):
		next_cycle_boss_health[boss_num+1] = health

	group.now_cycle_boss_health = json.dumps(now_cycle_boss_health)
	group.next_cycle_boss_health = json.dumps(next_cycle_boss_health)
	group.boss_cycle = 1
	group.challenging_member_list = None
	group.subscribe_list = None
	group.challenging_start_time = 0

	group.save()
	if battle_id is None: battle_id = group.battle_id
	Clan_challenge.delete().where(Clan_challenge.gid == group_id, Clan_challenge.bid == battle_id).execute()
	_logger.info(f'群{group_id}的{battle_id}号存档已清空')

#切换会战数据记录档案
def switch_data_slot(self, group_id: Groupid, battle_id: int):
	"""
	切换到选择的档案并重置boss状态
	挑战数据应进行备份和确认
	在调用此函数之前，要先检查操作者权限。

	Args:
		group_id: QQ群号
		battle_id：选择的档案号
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	backups:Clan_group_backups = Clan_group_backups.get_or_create(
		group_id = group_id, 
		battle_id = group.battle_id)[0]
	restore:Clan_group_backups = Clan_group_backups.get_or_create(
		group_id = group_id, 
		battle_id = battle_id)[0]
	
	#备份
	backups_group_data = {
		"group_name": group.group_name,
		"privacy": group.privacy,
		"game_server": group.game_server,
		"notification": group.notification,
		"battle_id": group.battle_id,
		"threshold": group.threshold,
		"boss_cycle": group.boss_cycle,
		"now_cycle_boss_health": group.now_cycle_boss_health,
		"next_cycle_boss_health": group.next_cycle_boss_health,
		"challenging_member_list": group.challenging_member_list,
		"subscribe_list": group.subscribe_list,
		"challenging_start_time": group.challenging_start_time,
	}
	backups.group_data = json.dumps(backups_group_data)
	backups.save()

	#还原
	group.battle_id = battle_id
	if restore.group_data: #如果有备份数据则还原
		data:Clan_group = json.loads(restore.group_data)
		group.group_name = data["group_name"]
		group.privacy = data["privacy"]
		group.game_server = data["game_server"]
		group.notification = data["notification"]
		group.boss_cycle = data["boss_cycle"]
		group.now_cycle_boss_health = data["now_cycle_boss_health"]
		group.next_cycle_boss_health = data["next_cycle_boss_health"]
		group.challenging_member_list = data["challenging_member_list"]
		group.subscribe_list = data["subscribe_list"]
		group.challenging_start_time = data["challenging_start_time"]
	else:	#没有备份数据则新建
		now_cycle_boss_health = {}
		level = self._level_by_cycle(1, group.game_server)
		for boss_num, health in enumerate(self.bossinfo[group.game_server][level]):
			now_cycle_boss_health[boss_num+1] = health
		next_cycle_boss_health = {}
		level = self._level_by_cycle(2, group.game_server)
		for boss_num, health in enumerate(self.bossinfo[group.game_server][level]):
			next_cycle_boss_health[boss_num+1] = health
		
		group.now_cycle_boss_health = json.dumps(now_cycle_boss_health)
		group.next_cycle_boss_health = json.dumps(next_cycle_boss_health)
		group.boss_cycle = 1
		group.challenging_member_list = None
		group.subscribe_list = None
		group.challenging_start_time = 0

	group.save()
	_logger.info(f'群{group_id}切换至{battle_id}号存档')

#向指定个人私聊发送提醒
async def send_private_remind(self, member_list:List[QQid] = None, member_id:QQid = None, content: str = None):
	if member_list:
		for qqid in member_list:
			await asyncio.sleep(random.randint(3, 10))
			try:
				await self.api.send_private_msg(user_id=qqid, message=content)
				_logger.info(f'向{qqid}发送出刀提醒')
			except Exception as e:
				_logger.exception(e)
	elif member_id and member_id > 0:
		try:
			await self.api.send_private_msg(user_id=member_id, message=content)
			_logger.info(f'向{member_id}发送代刀提醒')
		except Exception as e:
			_logger.exception(e)

#发送出刀提醒
def send_remind(self,
				group_id: Groupid,
				member_list: List[QQid],
				sender: QQid,
				send_private_msg: bool = False):
	"""
	在调用此函数之前，要先检查操作者权限。

	Args:
		group_id: QQ群号
		member_list: 被提醒的成员QQ号列表
		sender: 发送者QQ号
		send_private_msg: 是否私聊发送
	"""
	sender_name = self._get_nickname_by_qqid(sender)
	if send_private_msg:
		asyncio.ensure_future(self.send_private_remind(
			member_list=member_list,
			content=f'{sender_name}提醒您及时完成今日出刀',
		))
	else:
		message = ' '.join(atqq(qqid) for qqid in member_list)
		asyncio.ensure_future(self.api.send_group_msg(
			group_id=group_id,
			message=message+f'\n=======\n{sender_name}提醒您及时完成今日出刀',
		))

#发送代刀提醒给被代刀的玩家
def behelf_remind(self, member_id, msg):
	asyncio.ensure_future(self.send_private_remind(member_id = member_id,content = msg))
#当前的boss状态
def boss_status_summary(self, group_id:Groupid) -> str:
	boss_summary = self.challenger_info(group_id)

	return boss_summary


#报刀
def challenge(self,
				group_id: Groupid,
				qqid: QQid,
				defeat: bool,
				damage = 0,
				behalfed:QQid = None,
				is_continue = False,
				*,
				boss_num = None,
				previous_day = False,
				) :
	"""
	记录对boss造成的伤害

	Args:
		group_id: QQ群号
		qqid: 发出记录伤害请求的成员的QQ号（可能是代刀）
		defeat: 是否是尾刀
		damage: 对boss造成的伤害
		behalfed: 真正造成伤害的成员的QQ号
		previous_day: 是否是昨天出的刀
	"""
	if (not defeat) and (damage is None): raise InputError('未击败boss需要提供伤害值')
	if (not defeat) and (damage < 0): raise InputError('伤害不可以是负数')
	user = User.get_or_none(qqid=qqid)
	if user is None: raise UserNotInGroup

	behalf = None
	if behalfed is not None:
		behalfed = int(behalfed)
		behalf = qqid
		qqid = behalfed
	if qqid == behalf: behalf = None

	#若已申请出刀且指定报刀boss，优先选择指定报刀boss
	if boss_num and self.check_blade(group_id, qqid):
		self.cancel_blade(group_id, qqid, send_web = False)
	#若已申请出刀未指定报刀boss，自动选择申请出刀的boss
	if not boss_num and self.check_blade(group_id, qqid):
		boss_num = self.get_in_boss_num(group_id, qqid)

	if not boss_num:
		raise GroupError('又不申请出刀又不说打哪个王，报啥子刀啊 (╯‵□′)╯︵┻━┻')
	if not self.check_blade(group_id, qqid):
		if behalf:
			self.apply_for_challenge(is_continue, group_id, behalf, boss_num, qqid, False)
		else:
			self.apply_for_challenge(is_continue, group_id, qqid, boss_num, behalf, False)

	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist

	boss_num = str(boss_num)
	boss_cycle = group.boss_cycle
	challenging_member_list = safe_load_json(group.challenging_member_list, {})
	now_cycle_boss_health = safe_load_json(group.now_cycle_boss_health, {})
	next_cycle_boss_health = safe_load_json(group.next_cycle_boss_health, {})
	real_cycle_boss_health = now_cycle_boss_health
	is_continue = is_continue or challenging_member_list[boss_num][str(qqid)]['is_continue']
	if now_cycle_boss_health[boss_num] == 0 and next_cycle_boss_health[boss_num] != 0:
		boss_cycle += 1
		real_cycle_boss_health = next_cycle_boss_health
	elif now_cycle_boss_health[boss_num] == 0 and next_cycle_boss_health[boss_num] == 0: 
		raise InputError('只能挑战2个周目内的同个boss')
	if (not defeat) and (damage >= real_cycle_boss_health[boss_num]):
		raise InputError('伤害超出剩余血量，如击败请使用尾刀')
	# if damage == 0:
	# 	damage = challenging_member_list[boss_num][str(qqid)]['damage']

	d, t = pcr_datetime(area = group.game_server)
	if previous_day:
		today_count = Clan_challenge.select().where(
			Clan_challenge.gid == group_id,
			Clan_challenge.bid == group.battle_id,
			Clan_challenge.challenge_pcrdate == d,
		).count()

		if today_count != 0: raise GroupError('今日报刀记录不为空，无法将记录添加到昨日')
		d -= 1
		t += 86400

	challenges = Clan_challenge.select().where(
		Clan_challenge.gid == group_id,
		Clan_challenge.qqid == qqid,
		Clan_challenge.bid == group.battle_id,
		Clan_challenge.challenge_pcrdate == d,
	).order_by(Clan_challenge.cid)

	challenges = list(challenges)
	finished = sum(bool(c.boss_health_remain or c.is_continue) for c in challenges)
	if finished >= 3:
		if previous_day: raise InputError('昨日上报次数已达到3次')
		raise InputError('今日上报次数已达到3次')
	#出了多少刀补偿
	all_cont_blade = sum(bool(c.is_continue) for c in challenges)
	#剩余多少刀补偿
	cont_blade = len(challenges) - finished - all_cont_blade
	if is_continue and cont_blade == 0:
		raise GroupError('您没有补偿刀')

	if defeat:
		boss_health_remain = 0
		challenge_damage = real_cycle_boss_health[boss_num]
		real_cycle_boss_health[boss_num] = 0
	else:
		boss_health_remain = real_cycle_boss_health[boss_num] - damage
		challenge_damage = damage
		real_cycle_boss_health[boss_num] -= damage

	challenge = Clan_challenge.create(
		gid=group_id,
		qqid=qqid,
		bid=group.battle_id,
		challenge_pcrdate=d,
		challenge_pcrtime=t,
		boss_cycle=boss_cycle,
		boss_num=boss_num,
		boss_health_remain=boss_health_remain,
		challenge_damage=challenge_damage,
		is_continue=is_continue,
		behalf=behalf,
	)

	if defeat:
		all_clear = 0
		for _, _health in now_cycle_boss_health.items():
			if _health == 0: all_clear += 1
		if all_clear == 5:			# 检查当前周目的boss是否已经全部击杀
			group.boss_cycle += 1	# 进入下一周目
			next_cycle_level = self._level_by_cycle(group.boss_cycle+1, group.game_server)
			for _boss_num, _health in next_cycle_boss_health.items():# 血量数据挪移
				now_cycle_boss_health[_boss_num] = _health
				if _health == 0: subscribe_remind(self, group_id, _boss_num)# 如果挪过来的血量为0，则发送预约提醒
			for boss_num_, health_ in enumerate(self.bossinfo[group.game_server][next_cycle_level]):# 获取新血量数据放到下周目
				next_cycle_boss_health[str(boss_num_+1)] = health_
		else: real_cycle_boss_health[boss_num] = 0

	group.now_cycle_boss_health = json.dumps(now_cycle_boss_health)
	group.next_cycle_boss_health = json.dumps(next_cycle_boss_health)
	challenge.save()
	group.save()

	# 取消申请出刀
	if defeat: 
		self.take_it_of_the_tree(group_id, qqid, boss_num, 1, send_web = False)#只是通知下树而已
		self.cancel_blade(group_id, qqid, boss_num, 2, False)
		if check_next_boss(self, group_id, boss_num):
			subscribe_remind(self, group_id, boss_num)
	else:
		try:self.cancel_blade(group_id, qqid, send_web = False)
		except:pass

	nik = self._get_nickname_by_qqid(qqid)
	behalf_nik = behalf and f'（{self._get_nickname_by_qqid(behalf)}代）' or ''
	if defeat:
		msg = '{}{}对{}号boss造成了{:,}点伤害，击败了boss\n（今日第{}刀，{}）\n'.format(
			nik, behalf_nik, boss_num, challenge_damage, finished+1, '尾余刀' if is_continue else '收尾刀')
	else:
		msg = '{}{}对{}号boss造成了{:,}点伤害\n（今日第{}刀，{}）\n'.format(
			nik, behalf_nik, boss_num, challenge_damage, finished+1, '剩余刀' if is_continue else '完整刀')
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	msg += '\n'.join(self.challenger_info_small(group, boss_num))

	self._boss_status[group_id].set_result((self._boss_data_dict(group), group.boss_cycle, msg))
	self._boss_status[group_id] = asyncio.get_event_loop().create_future()

	return msg

#撤销上一刀的伤害
def undo(self, group_id: Groupid, qqid: QQid) :
	"""
	删除上一刀的记录

	Args:
		group_id: QQ群号
		qqid: 发起撤销请求的成员QQ号
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	user:User = User.get_or_create(qqid = qqid, defaults = {'clan_group_id': group_id})[0]
	last_challenge:Clan_challenge = self._get_group_previous_challenge(group)

	if last_challenge is None: raise GroupError('本群无出刀记录')
	if (last_challenge.qqid != qqid) and (user.authority_group >= 100): raise UserError('无权撤销')

	last_num = str(last_challenge.boss_num)	#上一刀的boss_num
	last_cycle = last_challenge.boss_cycle	#上一刀的周目数
	level = self._level_by_cycle(last_cycle, group.game_server)#阶段

	now_cycle_boss_health = safe_load_json(group.now_cycle_boss_health, {})
	next_cycle_boss_health = safe_load_json(group.next_cycle_boss_health, {})
	real_cycle_boss_health = now_cycle_boss_health #用来记录上一刀打的是哪个周目的boss

	if last_cycle < group.boss_cycle:	# 判断被撤销的一刀是否是切换周目的一刀
		for boss_num, health in now_cycle_boss_health.items():
			next_cycle_boss_health[boss_num] = health
			now_cycle_boss_health[boss_num] = 0
		now_cycle_boss_health[last_num] = last_challenge.challenge_damage
		group.boss_cycle = last_cycle
	else:
		if last_cycle != group.boss_cycle: real_cycle_boss_health = next_cycle_boss_health
		real_cycle_boss_health[last_num] += last_challenge.challenge_damage
		full_health = self.bossinfo[group.game_server][level][int(last_num)-1]
		if real_cycle_boss_health[last_num] > full_health: real_cycle_boss_health[last_num] = full_health

	last_challenge.delete_instance()
	group.now_cycle_boss_health = json.dumps(now_cycle_boss_health)
	group.next_cycle_boss_health = json.dumps(next_cycle_boss_health)
	group.save()

	nik = self._get_nickname_by_qqid(last_challenge.qqid)
	msg = f'{nik}的出刀记录已被撤销'
	self._boss_status[group_id].set_result((self._boss_data_dict(group), group.boss_cycle, msg))
	self._boss_status[group_id] = asyncio.get_event_loop().create_future()
	return msg

#预约x/预约表
def subscribe(self, group_id:Groupid, qqid:QQid, msg):
	"""
	预约某个boss或查看所有已预约的玩家

	Args:
		msg: 第几个王 or '表'
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	if not msg: GroupError('您预约了一个空气')
	if msg == '表':
		back_msg = []
		if group.subscribe_list is None:
			raise GroupError('目前没有人预约任意一个boss')
		subscribe_list = safe_load_json(group.subscribe_list, {})
		for boss_num in range(5):
			real_num = str(boss_num + 1)
			boss_msg = f'{real_num}王：'
			if real_num in subscribe_list:
				for boss_qqid in subscribe_list[real_num]:
					boss_msg += f'{self._get_nickname_by_qqid(int(boss_qqid))} '
			else:
				boss_msg += '无人预约'
			back_msg.append(boss_msg)
		return '\n'.join(back_msg)
	else:
		subscribe_list = safe_load_json(group.subscribe_list, {})
		boss_num = msg
		if boss_num in subscribe_list:
			if qqid in subscribe_list[boss_num]:
				raise GroupError('你已经预约过这个boss啦 (╯‵□′)╯︵┻━┻')
			subscribe_list[boss_num].append(qqid)
		else:
			subscribe_list[boss_num] = [qqid]
		group.subscribe_list = json.dumps(subscribe_list)
		group.save()
		return f'预约{boss_num}王成功！下个{boss_num}王出现时会at提醒。'

#预约提醒
def subscribe_remind(self, group_id:Groupid, boss_num):
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	subscribe_list = safe_load_json(group.subscribe_list, {})
	if len(subscribe_list) == 0 or boss_num not in subscribe_list: return
	qqid_list = subscribe_list[boss_num]
	asyncio.ensure_future(self.api.send_group_msg(
		group_id = group_id,
		message = f'船新的{boss_num}王来惹~ _(:з)∠)_\n' + ' '.join(atqq(qqid) for qqid in qqid_list),
	))
	subscribe_cancel(self, group_id, boss_num)

#取消预约
def subscribe_cancel(self, group_id:Groupid, boss_num, qqid = None):
	'''
	取消预约特定boss

	Args:
		boss_num: 几王
		qqid: 不填为删除特定boss的整个预约记录，填则删除特定用户的单个预约记录
	'''
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	subscribe_list = safe_load_json(group.subscribe_list, {})
	if not boss_num: GroupError('您取消了个寂寞')
	if len(subscribe_list) == 0 or boss_num not in subscribe_list:
		raise GroupError('您还没有预约这个boss')
	if not qqid:
		del subscribe_list[boss_num]
	else:
		if qqid not in subscribe_list[boss_num]:
			raise GroupError('您还没有预约这个boss')
		subscribe_list[boss_num].remove(qqid)
		if len(subscribe_list[boss_num]) == 0:
			del subscribe_list[boss_num]
	group.subscribe_list = json.dumps(subscribe_list)
	group.save()
	return '取消成功~'

#获取预约列表
def get_subscribe_list(self, group_id: Groupid):
	"""
	返回预约列表

	Args:
		group_id: QQ群号
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	subscribe_list = safe_load_json(group.subscribe_list, {})
	back_info = []
	for boss_num, qqid_list in subscribe_list.items():
		back_info.append({
			'boss': int(boss_num),
			'qqid': qqid_list,
			'message': None,
		})
	return back_info

#挂树
def put_on_the_tree(self, group_id: Groupid, qqid: QQid, message=None):
	"""
	放在树上

	Args:
		group_id: QQ群号
		qqid: 挂树的霉b/菜b的QQ号
		message: 留言
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	user = User.get_or_none(qqid=qqid)
	if user is None: raise GroupError('请先加入公会')
	if not self.check_blade(group_id, qqid):
		raise GroupError('你都没申请出刀，挂啥子树啊 (╯‵□′)╯︵┻━┻')

	challenging_member_list = safe_load_json(group.challenging_member_list, {})
	boss_num = self.get_in_boss_num(group_id, qqid)
	if not boss_num :
		raise GroupError('你都没申请出刀，挂啥子树啊 (╯‵□′)╯︵┻━┻')
	if challenging_member_list[boss_num][str(qqid)]['tree']:
		raise GroupError('您已经在树上了')
	
	challenging_member_list[boss_num][str(qqid)]['tree'] = True
	challenging_member_list[boss_num][str(qqid)]['msg'] = message
	group.challenging_member_list = json.dumps(challenging_member_list)
	group.save()
	self._boss_status[group_id].set_result((self._boss_data_dict(group), group.boss_cycle, '挂树惹~ (っ °Д °;)っ'))
	self._boss_status[group_id] = asyncio.get_event_loop().create_future()
	return '挂树惹~ (っ °Д °;)っ'

#下树
def take_it_of_the_tree(self, group_id: Groupid, qqid: QQid, boss_num=0, take_it_type = 0, send_web = True):
	"""
	把ta从树上取下来

	Args:
		group_id: QQ群号
		qqid: 挂树的霉b/菜b的QQ号
		boss_num: 砍一棵树
		take_it_type: 0下一个人 1下一棵树
		send_web:是否更新web面板数据
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	
	user = User.get_or_none(qqid=qqid)
	if user is None: raise GroupError('请先加入公会')

	challenging_member_list = safe_load_json(group.challenging_member_list, {})

	if take_it_type == 0:
		boss_num = self.get_in_boss_num(group_id, qqid)
		if not boss_num :
			raise GroupError('你都没申请出刀，下啥子树啊 (╯‵□′)╯︵┻━┻')
		qqid = str(qqid)
		challenging_member_list[boss_num][qqid]['tree'] = False
		challenging_member_list[boss_num][qqid]['msg'] = None
		group.challenging_member_list = json.dumps(challenging_member_list)
		group.save()
	elif take_it_type == 1:
		notice = []
		for challenger, info in challenging_member_list[boss_num].items():
			if info['tree']: notice.append(atqq(challenger))
		if len(notice) > 0:
			asyncio.ensure_future(self.api.send_group_msg(
				group_id = group_id,
				message = '可以下树惹~ _(:з)∠)_\n'+'\n'.join(notice),
			))
	if send_web:
		self._boss_status[group_id].set_result((self._boss_data_dict(group), group.boss_cycle, '下树惹~ _(:з)∠)_'))
		self._boss_status[group_id] = asyncio.get_event_loop().create_future()
	return '下树惹~ _(:з)∠)_'

#检查能否继续挑战下个boss
def check_next_boss(self, group_id:Groupid, boss_num):
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	boss_cycle = group.boss_cycle
	now_cycle_boss_health = safe_load_json(group.now_cycle_boss_health, {})
	next_cycle_boss_health = safe_load_json(group.next_cycle_boss_health, {})
	if now_cycle_boss_health[boss_num] == 0 and next_cycle_boss_health[boss_num] == 0:
		return False
	if self._level_by_cycle(boss_cycle, group.game_server) != self._level_by_cycle(boss_cycle+1, group.game_server):
		return False
	return True

#申请出刀
def apply_for_challenge(self, is_continue, group_id:Groupid, qqid:QQid, boss_num, behalfed = None, send_web=True) :
	"""
	Args:
		is_continue: 是否是补偿刀
		group_id: QQ群号
		qqid: 申请人的QQ号
		boss_num: 几王
		behalfed: 被代刀人的qq号
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None:raise GroupNotExist

	behalf = None
	challenger = behalfed and behalfed or qqid
	if behalfed : behalf = qqid
	user = User.get_or_none(challenger)
	if user is None:raise UserNotInGroup

	if self.check_blade(group_id, challenger):
		raise GroupError('你已经申请过了 (╯‵□′)╯︵┻━┻')

	now_cycle_boss_health = safe_load_json(group.now_cycle_boss_health, {})
	if (not check_next_boss(self, group_id, boss_num) 
		and now_cycle_boss_health[boss_num] == 0):
		raise GroupError('只能挑战2个周目内且不跨阶段的同个boss，请等待该周目的boss全部击杀完毕')

	d, _ = pcr_datetime(area = group.game_server)
	challenges = Clan_challenge.select().where(
		Clan_challenge.gid == group_id,
		Clan_challenge.qqid == challenger,
		Clan_challenge.bid == group.battle_id,
		Clan_challenge.challenge_pcrdate == d,
	).order_by(Clan_challenge.cid)
	challenges = list(challenges)
	finished = sum(bool(c.boss_health_remain or c.is_continue) for c in challenges)
	if finished >= 3: raise GroupError('今日已出了3次完整刀')
	#收尾且不是补偿
	tail_blade = sum(bool(c.boss_health_remain == 0 and (not c.is_continue)) for c in challenges)
	#出了多少刀补偿
	all_cont_blade = sum(bool(c.is_continue) for c in challenges)
	#剩余多少刀补偿
	cont_blade = len(challenges) - finished - all_cont_blade
	if is_continue and cont_blade == 0:
		raise GroupError('您没有补偿刀')
	if finished + tail_blade - all_cont_blade >= 3 and cont_blade != 0:
		is_continue = True
	
	nik = self._get_nickname_by_qqid(challenger)
	info = [f'{nik}已开始挑战boss，剩最后几秒的时候记得暂停报伤害哦~']
	challenging_list = safe_load_json(group.challenging_member_list, {})
	if boss_num not in challenging_list:
		challenging_list[boss_num] = {}
	challenging_list[boss_num][challenger] = {
		'is_continue' : is_continue, 
		'behalf' : behalf, 
		's' : 0,
		'damage' : 0,
		'tree' : False,
		'msg' : None,
	}
	group.challenging_member_list = json.dumps(challenging_list)
	group.save()

	self.challenger_info_small(group, boss_num, info)
	info = '\n'.join(info)
	if send_web:
		self._boss_status[group_id].set_result((self._boss_data_dict(group), group.boss_cycle, f'申请挑战{boss_num}王成功'))
		self._boss_status[group_id] = asyncio.get_event_loop().create_future()
	return info

#取消申请出刀
def cancel_blade(self, group_id: Groupid, qqid: QQid, boss_num=0, cancel_type=1, send_web=True):
	"""
	Args:
		group_id: QQ群号
		qqid: 需要进行操作的QQ号
		cancel_type: 取消类型：0取消全部 1取消特定qq号 2取消特定boss
		send_web:是否更新web面板数据
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	ret = 0
	if group.challenging_member_list == None:
		raise GroupError('目前没有人正在挑战这个boss')
	if cancel_type == 0 :
		group.challenging_member_list = None
		ret = '已取消所有'
	elif cancel_type == 1 :
		_boss_num = self.get_in_boss_num(group_id, qqid)
		if not _boss_num : raise GroupError('你都没申请出刀，取啥子消啊 (╯‵□′)╯︵┻━┻')
		challenging_list = safe_load_json(group.challenging_member_list, {})
		del challenging_list[_boss_num][str(qqid)]
		if len(challenging_list[_boss_num]) == 0: del challenging_list[_boss_num]
		if len(challenging_list) == 0: group.challenging_member_list = None
		else: group.challenging_member_list = json.dumps(challenging_list)
		ret = '取消申请出刀成功'
	elif boss_num != 0 and cancel_type == 2:
		challenging_list = safe_load_json(group.challenging_member_list, {})
		if boss_num not in challenging_list: return
		del challenging_list[boss_num]
		group.challenging_member_list = json.dumps(challenging_list)

	if send_web:
		self._boss_status[group_id].set_result((self._boss_data_dict(group), group.boss_cycle, ret))
		self._boss_status[group_id] = asyncio.get_event_loop().create_future()
	group.save()
	return ret

#检查是否已申请出刀
def check_blade(self, group_id: Groupid, qqid: QQid):
	"""
	返回False即已申请出刀，返回True为未申请出刀
	Args:
		group_id: QQ群号
		qqid: 需要进行操作的QQ号
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	challenging_list = safe_load_json(group.challenging_member_list, {})
	for _, infos in challenging_list.items():
		for challenger in infos.keys():
			if str(qqid) == challenger : return True
	return False

#获取boss_num
def get_in_boss_num(self, group_id, qqid):
	"""
	获取已申请出刀的qqid正在挑战哪个王（挂在哪棵树（不是）），返回False为未找到
	Args:
		group: 公会群对象
		qqid: 需要进行操作的QQ号
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	challenging_list = safe_load_json(group.challenging_member_list, {})
	for boss_num, infos in challenging_list.items():
		for challenger in infos.keys():
			if str(qqid) == challenger : return boss_num
	return False


#SL
def save_slot(self, group_id: Groupid, qqid: QQid,
				only_check: bool = False,
				clean_flag: bool = False):
	"""
	记录今天的sl情况

	Args:
		group_id: QQ群号
		qqid: 需要进行操作的QQ号
		todaystatus: 今日状态
		only_check: 是否只查询
		clean_flag: 是否取消sl
	"""
	group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	membership = Clan_member.get_or_none(group_id = group_id, qqid = qqid)
	if membership is None: raise UserNotInGroup
	today, _ = pcr_datetime(group.game_server)
	if clean_flag:
		if membership.last_save_slot != today: raise UserError('您今天还没有SL过')
		membership.last_save_slot = 0
		membership.save()
		return '已取消SL'
	if only_check:
		return (membership.last_save_slot == today)
	if membership.last_save_slot == today:
		raise UserError('您今天已经SL过了，该不会退游戏了吧 Σ(っ °Д °;)っ')
	membership.last_save_slot = today
	membership.save()

	# refresh
	self.get_member_list(group_id, nocache = True)
	return 'SL用掉惹 Σ(っ °Д °;)っ'

#记录伤害/清空伤害
def report_hurt(self, s, hurt, group_id:Groupid, qqid:QQid, clean_type = 0):
	"""
	记录/清空出刀暂停后，成员报的伤害

	Args:
		s: 秒
		hurt: 伤害
		group_id: QQ群号
		qqid: 需要进行操作的QQ号
		clean_type: 清理类型 0不清理(记录伤害) 1清特定玩家
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	boss_num = self.get_in_boss_num(group_id, qqid)
	if clean_type != 2 and not boss_num:
		raise GroupError('你都没申请出刀，报啥子伤害啊 (╯‵□′)╯︵┻━┻')

	ret_msg = ''
	challenging_member_list = safe_load_json(group.challenging_member_list, {})

	str_qqid = str(qqid)
	if clean_type == 0:
		challenging_member_list[boss_num][str_qqid]['s'] = s
		challenging_member_list[boss_num][str_qqid]['damage'] = hurt
		ret_msg = '已记录伤害，小心不要手滑哦~ ♪(´▽｀)'
	elif clean_type == 1:
		if challenging_member_list[boss_num][str_qqid]['damage'] == 0:
			ret_msg = '您还没有报伤害呢'
		else:
			challenging_member_list[boss_num][str_qqid]['s'] = 0
			challenging_member_list[boss_num][str_qqid]['damage'] = 0
			ret_msg = '取消成功~'

	group.challenging_member_list = json.dumps(challenging_member_list)
	group.save()
	return ret_msg

#单个boss信息
def challenger_info_small(self, group:Clan_group, boss_num, msg:List = None):
	"""
	Args:
		group: 公会信息对象
		boss_num: 几王
	"""
	now_health = safe_load_json(group.now_cycle_boss_health)[boss_num]
	next_health = safe_load_json(group.next_cycle_boss_health)[boss_num]

	challenging_list = safe_load_json(group.challenging_member_list)
	if challenging_list and (boss_num in challenging_list): 
		challenging_list = challenging_list[boss_num]
	else:
		challenging_list = None

	real_health = next_health if now_health == 0 else now_health
	real_health_str = '{:,}'.format(real_health)
	cycle = group.boss_cycle + 1 if now_health == 0 else group.boss_cycle
	if not msg: msg = []
	msg.append(f'{cycle}周目{boss_num}王，剩余{real_health_str}血')
	if now_health == 0 and not check_next_boss(self, group.group_id, boss_num):
		msg.append(f'该boss无法继续挑战')
		return msg
	elif not challenging_list or len(challenging_list) == 0:
		msg.append(f'目前无人在挑战这个boss')
		return msg
	else:
		msg.append(f'当前有{len(challenging_list)}人正在挑战这个boss')

	if challenging_list:
		msg.append('--------------------')
		for challenger, info in challenging_list.items():
			temp_msg = f'— {self._get_nickname_by_qqid(int(challenger))}'
			if info['is_continue']:
				temp_msg += '(补偿)'
			if info['behalf']:
				behalf = self._get_nickname_by_qqid(info['behalf'])
				temp_msg += f'({behalf}代刀)'
			if info['damage'] > 0:
				temp_msg += f', 剩{info["s"]}秒，打了{info["damage"]}万伤害'
			if info['tree']:
				temp_msg += ', 已挂树'
			msg.append(temp_msg)
		msg.append('--------------------')

	return msg

#总出刀信息
def challenger_info(self, group_id):
	"""
	Args:
		group: 公会信息对象
	"""
	group:Clan_group = Clan_group.get_or_none(group_id=group_id)
	if group is None : raise GroupNotExist
	date, time = pcr_datetime(area = group.game_server)
	challenges = Clan_challenge.select().where(
					Clan_challenge.gid == group_id,
					Clan_challenge.bid == group.battle_id,
					Clan_challenge.challenge_pcrdate == date,
				).order_by(Clan_challenge.cid)
	end_blade_qqid = {}         #保存有尾刀未出的人的qq
	for c in challenges:
		#如果出完这刀时boss的血量为0，且不是收尾刀
		if c.boss_health_remain == 0 and not c.is_continue:
			if c.qqid not in end_blade_qqid:
				end_blade_qqid[c.qqid] = 1
			else:
				end_blade_qqid[c.qqid] += 1
		if c.is_continue and c.qqid in end_blade_qqid:
			end_blade_qqid[c.qqid] -= 1
			if end_blade_qqid[c.qqid] == 0: del end_blade_qqid[c.qqid]

	line = 0
	finished = sum(bool(c.boss_health_remain or c.is_continue) for c in challenges)
	msg = [f'今天公会已出{finished}刀完整刀']
	if len(end_blade_qqid) > 0 :
		temp_msg = ''
		for qqid, num in end_blade_qqid.items() :
			if num > 0: temp_msg += f'{self._get_nickname_by_qqid(qqid)}*{num}，'
		temp_msg += f'还有补偿刀未出'
		msg.append(temp_msg)
	
	msg.append('====================')
	for boss_num in range(5):
		self.challenger_info_small(group, str(boss_num+1), msg)
		msg.append('====================')
	for once in range(len(msg)):
		str_list = list(msg[once])
		for i in range(math.floor(len(msg[once])/21)):
			str_list.insert((i+1)*20, '\n')
			line += 1
		msg[once] = ''.join(str_list)
	back_msg = text_2_pic(self, '\n'.join(msg), 250, (len(msg)+line)*20 + 10, (255, 255, 255), "#000000", 15, (10, 5))
	
	return back_msg



##获取报告
@timed_cached_func(max_len=64, max_age_seconds=10, ignore_self=True)
def get_report(self,
				group_id: Groupid,
				battle_id: Union[str, int, None],
				qqid: Optional[QQid] = None,
				pcrdate: Optional[Pcr_date] = None
				) -> ClanBattleReport:
	"""
	get the records

	Args:
		group_id: QQ群号
		qqid: user id of report
		pcrdate: pcrdate of report
	"""
	group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	report = []
	expressions = [
		Clan_challenge.gid == group_id,
	]
	if battle_id is None:
		battle_id = group.battle_id
	if isinstance(battle_id, str):
		if battle_id == 'all':
			pass
		else:
			raise InputError(
				f'unexceptd value "{battle_id}" for battle_id')
	else:
		expressions.append(Clan_challenge.bid == battle_id)
	if qqid is not None:
		expressions.append(Clan_challenge.qqid == qqid)
	if pcrdate is not None:
		expressions.append(Clan_challenge.challenge_pcrdate == pcrdate)
	for c in Clan_challenge.select().where(
		*expressions
	):
		report.append({
			'battle_id': c.bid,
			'qqid': c.qqid,
			'challenge_time': pcr_timestamp(
				c.challenge_pcrdate,
				c.challenge_pcrtime,
				group.game_server,
			),
			'challenge_pcrdate': c.challenge_pcrdate,
			'challenge_pcrtime': c.challenge_pcrtime,
			'cycle': c.boss_cycle,
			'boss_num': c.boss_num,
			'health_remain': c.boss_health_remain,
			'damage': c.challenge_damage,
			'is_continue': c.is_continue,
			'message': c.message,
			'behalf': c.behalf,
		})
	return report

#从会战记录里获取成员列表
@timed_cached_func(max_len=64, max_age_seconds=10, ignore_self=True)
def get_battle_member_list(self,
							group_id: Groupid,
							battle_id: Union[str, int, None],
							):
	"""
	Args:
		group_id: QQ群号
		battle_id: 会战记录编号
	"""
	group = Clan_group.get_or_none(group_id=group_id)
	if group is None: raise GroupNotExist
	expressions = [
		Clan_challenge.gid == group_id,
	]
	if battle_id is None:
		battle_id = group.battle_id
	if isinstance(battle_id, str):
		if battle_id == 'all':
			pass
		else:
			raise InputError(
				f'unexceptd value "{battle_id}" for battle_id')
	else:
		expressions.append(Clan_challenge.bid == battle_id)
	member_list = []
	for u in Clan_challenge.select(
		Clan_challenge.qqid,
		User.nickname,
	).join(
		User,
		on=(Clan_challenge.qqid == User.qqid),
		attr='user',
	).where(
		*expressions
	).distinct():
		member_list.append({
			'qqid': u.qqid,
			'nickname': u.user.nickname,
		})
	return member_list

#获取并刷新成员列表
@timed_cached_func(max_len=16, max_age_seconds=3600, ignore_self=True)
def get_member_list(self, group_id: Groupid) -> List[Dict[str, Any]]:
	"""
	获取并刷新成员列表

	Args:
		group_id: QQ群号
	"""
	member_list = []
	for user in User.select(
		User, Clan_member,
	).join(
		Clan_member,
		on=(User.qqid == Clan_member.qqid),
		attr='clan_member',
	).where(
		Clan_member.group_id == group_id,
		User.deleted == False,
	):
		member_list.append({
			'qqid': user.qqid,
			'nickname': user.nickname,
			'sl': user.clan_member.last_save_slot,
		})
	return member_list




