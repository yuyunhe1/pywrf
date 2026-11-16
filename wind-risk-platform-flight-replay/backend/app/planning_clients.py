"""One latest planning request per browser page, including pending POSTs."""
from threading import RLock
from time import monotonic


class PlanningSuperseded(ValueError):
    pass


class PlanningClients:
    def __init__(self,*managers):
        self.managers=managers
        self.pages={}
        self.lock=RLock()

    def _page(self,client):
        now=monotonic()
        for key,value in list(self.pages.items()):
            if now-value['seen']>3600: del self.pages[key]
        page=self.pages.setdefault(client,{'latest':0,'cancelled':0,'bound':None})
        page['seen']=now
        return page

    def begin(self,request):
        client,sequence=request.client_instance_id,request.client_request_sequence
        if not client or sequence is None: return None
        with self.lock:
            page=self._page(client)
            if sequence<=page['cancelled'] or sequence<page['latest']:
                raise PlanningSuperseded('旧规划请求已取消或已被新请求替代')
            if sequence>page['latest']:
                for manager in self.managers: manager.cancel_client(client,sequence-1,forget=True)
                page.update(latest=sequence,bound=None)
            return client,sequence

    def submit(self,token,manager,submit):
        if token is None: return submit()
        client,sequence=token
        with self.lock:
            page=self._page(client)
            if sequence!=page['latest'] or sequence<=page['cancelled']:
                raise PlanningSuperseded('旧规划请求已取消或已被新请求替代')
            if page['bound'] is not None:
                owner,job_id=page['bound']
                return owner.snapshot(job_id)
            state=submit()
            page['bound']=manager,state['job_id']
            return state

    def cancel(self,client,through=None,forget=False):
        with self.lock:
            page=self._page(client)
            through=page['latest'] if through is None else through
            page['cancelled']=max(page['cancelled'],through)
            finished=[manager.cancel_client(client,through,forget) for manager in self.managers]
            if forget and through>=page['latest']: page['bound']=None
            return {'client_instance_id':client,'cancelled_through':through,'compute_finished':all(finished)}
