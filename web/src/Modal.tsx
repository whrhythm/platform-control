import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import './modal.css';

export default function Modal({title,onClose,children}:{title:string;onClose:()=>void;children:ReactNode}) {
  const ref=useRef<HTMLDialogElement>(null);
  useEffect(()=>{const dialog=ref.current;dialog?.showModal();return ()=>dialog?.close();},[]);
  return <dialog ref={ref} className="console-modal" aria-label={title}
    onCancel={event=>{event.preventDefault();onClose();}}
    onClick={event=>{if(event.target===event.currentTarget)onClose();}}>
    <div className="console-modal-content"><header><h2>{title}</h2><button type="button" className="subtle" aria-label="关闭浮窗" onClick={onClose}>×</button></header>{children}</div>
  </dialog>;
}
