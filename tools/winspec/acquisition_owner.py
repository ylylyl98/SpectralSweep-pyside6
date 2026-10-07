"""Request-apartment COM references, including failed/by-reference Start outputs."""


def same_document(a,b):
    if a is b:return True
    try:
        import pythoncom
        return a._oleobj_.QueryInterface(pythoncom.IID_IUnknown)==b._oleobj_.QueryInterface(pythoncom.IID_IUnknown)
    except Exception:return False


class Owner(object):
    def __init__(self,exp,cancelled,on_start,decode):
        self.exp=exp;self.cancelled=cancelled;self.on_start=on_start;self.decode=decode
        self.documents=[];self.failed=False
    @property
    def acquisition_owner(self):return self
    def __getattr__(self,name):return getattr(self.exp,name)
    def retain(self,doc,attempted=False):
        for row in self.documents:
            if same_document(row['doc'],doc):row['attempted']|=attempted;return
        self.documents.append(dict(doc=doc,attempted=attempted,closed=False))
    def Start(self,doc):
        self.retain(doc)
        if self.cancelled():raise RuntimeError('WinSpec acquisition stopped before Start')
        self.on_start()
        if self.cancelled():raise RuntimeError('WinSpec acquisition stopped before Start')
        self.retain(doc,True)
        result=self.exp.Start(doc)
        if isinstance(result,(tuple,list)):
            for candidate in result[1:]:
                if callable(getattr(candidate,'SaveAs',None)) and callable(getattr(candidate,'Close',None)):
                    self.retain(candidate,True)
        if self.cancelled():raise RuntimeError('WinSpec acquisition stopped during Start')
        return result
    def mark_closed(self,doc):
        for row in self.documents:
            if same_document(row['doc'],doc):row['closed']=True
    def cleanup_rejected(self,path_factory):
        paths=[]
        try:
            for i,row in enumerate(self.documents):
                if row['closed']:continue
                doc=row['doc']
                if row['attempted']:
                    path=path_factory(i)
                    if not self.decode(doc.SaveAs(path,1),'SaveAs'):raise RuntimeError('Could not archive rejected owned document')
                    paths.append(path)
                    if not self.decode(doc.Save(),'Save'):raise RuntimeError('Could not save rejected owned document')
                if not self.decode(doc.Close(),'Close'):raise RuntimeError('Could not close owned document')
                row['closed']=True
        except BaseException:
            self.failed=True;raise
        self.documents=[];self.exp=None
        return paths
