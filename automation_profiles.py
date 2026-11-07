"""Bind each role to one extension installation, independent of Chrome names."""
import json
import time
import uuid

from automation_engine import atomic


class ProfileBindings:
    def __init__(self,engine):
        self.engine=engine
        self.path=engine.home/'profile-bindings.json'
        self.data=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
            'instances':{'main':{},'gpt':{}},'active':{'main':None,'gpt':None}}

    @staticmethod
    def identifier(value):
        try:return uuid.UUID(str(value)).hex
        except (ValueError,TypeError,AttributeError):raise ValueError('Extension installation ID is missing; reload the current extension') from None

    def mutable(self,role):
        self.engine.assert_profile_idle(role)

    def register(self,role,value,label=''):
        instance=self.identifier(value)
        with self.engine.lock:
            entries=self.data['instances'][role]
            fresh=instance not in entries
            first=not entries
            profile=getattr(self.engine.settings,role+'_profile')
            if fresh:
                from flow_music_browser import chrome_profiles
                metadata=next((p for p in chrome_profiles() if p['directory']==profile),{})
                display=metadata.get('name') or profile
                entries[instance]={'id':instance,'name':label or f'{role} · {display} ({profile}) · {instance[:6]}','profile':profile}
            if label:entries[instance]['name']=label[:100]
            entries[instance]['last_seen']=time.time()
            # Auto-pair the first installation only. A replacement never steals the role.
            if fresh and first:self.data['active'][role]=instance
            if fresh or label:atomic(self.path,self.data)
            return instance

    def selected(self,role,instance):
        return self.data['active'][role]==instance

    def clear(self,role):
        with self.engine.lock:
            self.mutable(role)
            self.data['active'][role]=None
            self.engine.connected.pop(role,None)
            atomic(self.path,self.data)

    def activate(self,role,value):
        instance=self.identifier(value)
        with self.engine.lock:
            if self.selected(role,instance):return instance
            self.mutable(role)
            if instance not in self.data['instances'][role]:raise ValueError('Extension chưa đăng ký. Mở popup extension ở profile mới trước.')
            self.data['active'][role]=instance
            self.engine.connected[role]=self.data['instances'][role][instance].get('last_seen',0)
            atomic(self.path,self.data)
            return instance

    def rename(self,role,value,label):
        instance=self.identifier(value)
        with self.engine.lock:
            if instance not in self.data['instances'][role]:raise ValueError('Unknown extension installation')
            self.data['instances'][role][instance]['name']=label.strip()[:100] or instance[:6]
            atomic(self.path,self.data)

    def public(self):
        with self.engine.lock:
            return {'active':dict(self.data['active']), 'instances':{
                role:[{**item,'online':time.time()-item.get('last_seen',0)<75} for item in entries.values()]
                for role,entries in self.data['instances'].items()}}
