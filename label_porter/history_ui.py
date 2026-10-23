"""Paginated local history browser shared by resume and report merging."""
import os
import queue
import threading
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from . import widgets as ttk
from .job_history import export_job
from .ui_lifecycle import UiEventQueue, release_variables

KINDS = {'draft': '진행 중 설정', 'preview': '검증 미리보기', 'run': '최종 처리', 'merge': '병합 결과서'}
STATES = {'completed': '완료', 'completed_with_issues': '완료 · 검토 있음', 'failed': '실패', 'cancelled': '중단', 'running': '처리 중'}


class JobList(ttk.Frame):
    def __init__(self, parent, store, *, reports_only=False, select=None):
        super().__init__(parent)
        self.store, self.reports_only, self.page, self.entries = store, reports_only, 0, {}
        self.search = tk.StringVar(); self.kind = tk.StringVar(value='전체'); self.info = tk.StringVar()
        bar = ttk.Frame(self); bar.pack(fill='x', pady=(0,8))
        ttk.Label(bar, text='작업 검색').pack(side='left', padx=(0,8))
        entry = ttk.Entry(bar, textvariable=self.search); entry.pack(side='left', fill='x', expand=True)
        entry.bind('<Return>', lambda e:self.refresh(reset=True))
        ttk.Button(bar, text='검색', command=lambda:self.refresh(reset=True)).pack(side='left', padx=6)
        choices = ['전체'] + [v for k,v in KINDS.items() if not reports_only or k != 'draft']
        combo = ttk.Combobox(bar, values=choices, textvariable=self.kind, state='readonly', width=18)
        combo.pack(side='left'); combo.bind('<<ComboboxSelected>>', lambda e:self.refresh(reset=True))
        box = ttk.Frame(self); box.pack(fill='both', expand=True)
        self.table = ttk.Treeview(box, columns=('date','name','kind','status','records'), show='headings', height=9, selectmode='extended' if reports_only else 'browse')
        for key,title,width in [('date','저장 시각',155),('name','작업 이름 / 규칙',290),('kind','종류',120),('status','상태',150),('records','원본 / 보완',120)]:
            self.table.heading(key,text=title); self.table.column(key,width=width, minwidth=80)
        scroll=ttk.Scrollbar(box,command=self.table.yview); scroll.pack(side='right',fill='y')
        self.table.configure(yscrollcommand=scroll.set); self.table.pack(fill='both',expand=True)
        if select: self.table.bind('<<TreeviewSelect>>',lambda e:select(self.selected()))
        bar=ttk.Frame(self); bar.pack(fill='x',pady=8)
        ttk.Label(bar,textvariable=self.info).pack(side='left')
        self.next=ttk.Button(bar,text='다음 →',command=lambda:self.move(1)); self.next.pack(side='right')
        self.previous=ttk.Button(bar,text='← 이전',command=lambda:self.move(-1)); self.previous.pack(side='right',padx=6)
        self.bind('<Destroy>', lambda e: release_variables(self) if e.widget is self else None, add='+')

    def selected(self): return [self.entries[key] for key in self.table.selection() if key in self.entries]

    def refresh(self, reset=False):
        if reset:self.page=0
        kind = next((k for k,v in KINDS.items() if v==self.kind.get()),'')
        rows,count,self.page = self.store.list(self.search.get().strip(),kind,self.page,reports_only=self.reports_only)
        self.entries={r['id']:r for r in rows}; self.table.delete(*self.table.get_children())
        for r in rows:
            summary=r.get('summary',{})
            self.table.insert('','end',iid=r['id'],values=(r['updated'][:19].replace('T',' '),r['title'],KINDS[r['kind']],STATES.get(r['status'],r['status']),
                f"{summary.get('source_records', '—')} / {summary.get('result_records', '—')}"))
        self.info.set(f'{count:,}개 작업 · {self.page+1}/{max(1,(count+24)//25)}쪽 · 25개씩 표시')
        self.previous.configure(state='normal' if self.page else 'disabled')
        self.next.configure(state='normal' if (self.page+1)*25<count else 'disabled')

    def move(self,delta): self.page+=delta; self.refresh()


class HistoryPanel(ttk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,padding=12)
        self.app,self.store=app,app.history
        self.busy=self.closed=False; self.job=None; self.cancel=threading.Event(); self.events=UiEventQueue()
        self.message=tk.StringVar(value='작업 설정과 결과 위치는 자동 저장됩니다. 목록을 선택해 이어서 작업하거나 결과를 확인하세요.')
        self.detail=tk.StringVar(value='작업을 선택하세요.')
        primary=ttk.WrapBar(self);primary.pack(fill='x',pady=(0,8))
        self.list=JobList(self,self.store,select=self._selection); self.list.pack(fill='both',expand=True)
        self.list.table.bind('<Double-1>', lambda e: self._load() if self.list.table.identify_region(e.x,e.y) == 'cell' else None)
        self.list.table.bind('<Return>', lambda e: self._load())
        bar=ttk.WrapBar(self); bar.pack(fill='x',pady=8)
        self.buttons=[]
        for number,(title,callback) in enumerate([('작업 불러오기',self._load),('현재 설정 저장',self._save),('이름 변경',self._rename),('작업 삭제',self._delete),('HTML 결과서',lambda:self._open('html')),('Excel 결과서',lambda:self._open('excel')),('폴더 열기',lambda:self._open('folder')),('Excel 다른 이름으로 저장',lambda:self._export(True)),('결과 전체 ZIP 내보내기',lambda:self._export(False)),('결과 파일까지 삭제',self._delete_files),('보관함 새로고침',self.refresh),('기존 결과 폴더 등록',self._import)]):
            owner=primary if number<3 else bar
            b=ttk.Button(owner,text=title,command=callback,style='Primary.TButton' if number==0 else 'TButton');owner.add(b);self.buttons.append(b)
        ttk.Label(self,textvariable=self.detail,wraplength=950).pack(fill='x',pady=10)
        ttk.Label(self,textvariable=self.message,wraplength=950).pack(fill='x',pady=6)
        self.progress=ttk.Progressbar(self,mode='indeterminate');self.progress.pack(fill='x')
        self.bind('<Configure>',self._resize,add='+')
        self.bind('<Destroy>',self._destroyed,add='+')

    def _resize(self,event):
        rows=max(3,min(8,(self.winfo_toplevel().winfo_height()-600)//36+4))
        if int(self.list.table.cget('height'))!=rows:self.list.table.configure(height=rows)
        for child in self.winfo_children():
            if isinstance(child,ttk.Label):child.configure(wraplength=max(250,event.width-24))

    def _selected(self):
        rows=self.list.selected()
        if not rows:self.message.set('목록에서 작업을 선택하세요.');return None
        return rows[0]

    def _selection(self,rows):
        if not rows:return
        r=rows[0]; s=r.get('summary',{})
        self.detail.set(f"{r['title']} · {KINDS[r['kind']]}\n저장 위치: {r.get('directory','설정만 저장 · 데이터 복사 없음')}\n변경 필드 {s.get('changed_fields','—')} · 검토/오류 {s.get('issue_rows','—')}\n불러오기는 입력·규칙 설정을 복원합니다. 실행 전 실제 입력을 다시 확인합니다.")

    def refresh(self):
        if not self.busy:self.list.refresh()

    def _save(self):
        success=self.app._save_settings();self.refresh();self.message.set('현재 설정과 결과 위치를 저장했습니다. 데이터 파일은 다시 저장하지 않습니다.' if success else '설정을 저장하지 못했습니다. 하단 상태를 확인하세요.')

    def _load(self):
        row=self._selected()
        if row:self.app._load_history_job(row)

    def _delete(self):
        row=self._selected()
        if not row or self.busy:return
        if not messagebox.askyesno('작업 삭제',f'“{row["title"]}”을 보관함 목록에서 삭제할까요?\n결과 파일과 원본 데이터는 그대로 유지됩니다.',parent=self):return
        self.store.delete(row['id'])
        if row['id']==self.app.draft_id:self.app.draft_id='draft-'+uuid.uuid4().hex
        self.list.refresh();self.app.report_merger.refresh_history()
        self.detail.set('작업을 선택하세요.')
        self.message.set('보관함 목록에서 삭제했습니다. 데이터 파일은 변경하지 않았습니다.')

    def _delete_files(self):
        row=self._selected()
        if not row or self.busy:return
        target=self.store.managed_target(row)
        if target is None:
            self.message.set('파일 삭제는 프로그램 전용 보관함의 결과에만 사용할 수 있습니다. 직접 지정한 위치는 “작업 삭제”로 목록에서만 제거하세요.')
            return
        if not messagebox.askyesno('결과 파일까지 삭제',f'“{row["title"]}”의 프로그램 보관함 결과 폴더를 삭제할까요?\n\n{target}\n\n원본·보완 입력 파일은 삭제하지 않습니다.',parent=self):return
        def done():
            self.app._forget_deleted_result(row)
            self.detail.set('작업을 선택하세요.')
            self.app.report_merger.refresh_history()
        self.background(lambda check:f'결과 파일과 작업 기록을 삭제했습니다: {self.store.delete_managed_files(row["id"],check)}',
                        '프로그램 보관함의 결과 파일을 삭제하고 있습니다…',done)

    def _rename(self):
        row=self._selected()
        if not row:return
        name=simpledialog.askstring('작업 이름','작업 이름',initialvalue=row['title'],parent=self)
        if name and name.strip():self.store.rename(row['id'],name);self.refresh()

    def _open(self,kind):
        row=self._selected()
        if not row:return
        if not row.get('directory'):self.message.set('아직 생성된 결과물이 없습니다.');return
        folder=Path(row['directory'])
        path=folder if kind=='folder' else Path(row['report']) if row['kind']=='merge' and kind=='excel' else folder/'reports'/('report.html' if kind=='html' else 'report.xlsx')
        try:
            if not path.exists():raise FileNotFoundError('파일이 이동되었거나 아직 생성되지 않았습니다. 기존 결과 폴더 등록으로 위치를 다시 등록할 수 있습니다.')
            os.startfile(str(path))
        except OSError as exc:self.message.set(str(exc))

    def _export(self,excel):
        row=self._selected()
        if not row or not row.get('directory'):self.message.set('결과물이 있는 작업을 선택하세요.');return
        suffix='.xlsx' if excel else '.zip'
        target=filedialog.asksaveasfilename(parent=self,title='다른 이름으로 저장',initialfile='보완결과서'+suffix,defaultextension=suffix,filetypes=[('Excel' if excel else 'ZIP','*'+suffix)])
        if target:self.background(lambda check:export_job(row,target,excel=excel,checkpoint=check),'선택한 결과물을 내보내고 있습니다…')

    def _import(self):
        root=filedialog.askdirectory(parent=self,title='기존 처리 결과를 포함하는 폴더 등록 (파일 이동 없음)')
        if root:self.discover([root],restore=True)

    def discover(self,roots,restore=False):
        def work(check):
            count,errors=self.store.discover(roots,check,restore=restore)
            return f'기존 작업 {count}개 확인 · 파일 이동/복사 없음' + (f'\n등록 실패 {len(errors)}개: '+errors[0] if errors else '')
        self.background(work,'기존 결과 위치를 확인하고 있습니다…')

    def background(self,work,message,on_done=None):
        if self.busy:return
        self.on_done=on_done
        self.busy=True;self.cancel.clear();self.message.set(message);self.progress.start()
        for b in self.buttons:b.configure(state='disabled')
        events,cancel=self.events,self.cancel
        def worker():
            def check():
                if cancel.is_set():raise InterruptedError('작업이 중단되었습니다.')
            try:events.put(('done',work(check)))
            except Exception as exc:events.put(('error',str(exc)))
        threading.Thread(target=worker,daemon=False,name='job-library').start()
        self.job=self.after(100,self._poll)

    def _poll(self):
        self.job=None
        if self.closed:return
        try:
            kind,value=self.events.get_nowait();self.busy=False;self.progress.stop()
            for b in self.buttons:b.configure(state='normal')
            callback=self.on_done;self.on_done=None
            if kind=='done' and callback:callback()
            self.message.set(('완료 · ' if kind=='done' else '처리하지 못했습니다: ')+str(value));self.refresh()
            self.app.report_merger.refresh_history()
        except queue.Empty:pass
        if self.busy:self.job=self.after(100,self._poll)

    def _destroyed(self,event):
        if event.widget is not self:return
        self.closed=True;self.cancel.set();self.events.close()
        if self.job:self.after_cancel(self.job)
        release_variables(self)
