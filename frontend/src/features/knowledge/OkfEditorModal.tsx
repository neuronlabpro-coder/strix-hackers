import { useMemo, useState } from 'react'
import { FileCode2, PenLine, Save, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { KnowledgeDocType } from '../../types/api'
import {
  OKF_TYPES,
  TIPO_OKF_A_ENUM,
  type OkfType,
  componerOkf,
  extraerParaFormulario,
  validarOkf,
} from './OkfValidator'

/**
 * El modal de alta de un documento OKF, con dos formas de escribirlo.
 *
 * ## Por qué dos pestañas y no una
 *
 * Porque las dos atienden a personas distintas y no son la misma tarea con dos Skin. El
 * **editor** es para quien ya tiene el documento —lo ha copiado de otra herramienta, lo ha
 * heredado de un compañero— y quiere pegarlo tal cual sin que nada se modifique. El
 * **formulario** es para quien va a escribir de cero y no sabe qué claves exige el formato.
 *
 * Y la dirección importa más de lo que parece: es mucho más fácil **pegar** que **transcribir**.
 * Un documento real que ya existe en un wiki no se reescribe en un formulario, se pega; y
 * obligar a ese camino a pasar por cinco campos es convertir un pegado en un trabajo. Por eso el
 * editor es la pestaña que se abre por defecto.
 *
 * ## Por qué el botón de guardar está bloqueado con el documento inválido
 *
 * Porque el backend devuelve un error que nombra el campo, y ese error llega **después** de que
 * el usuario ha escrito el documento entero. Bloquear antes es lo mismo que hace un formulario
 * de navegador, y con la ventaja de que el mensaje explica el formato en vez de repetir
 * "revisa los campos marcados".
 */

export interface OkfEditorModalProps {
  onClose: () => void
  onSave: (payload: {
    title: string
    doc_type: KnowledgeDocType
    content: string
  }) => Promise<boolean>
  isSaving: boolean
  saveFailed: boolean
}

type Pestana = 'editor' | 'formulario'

const PLANTILLA = `---
type: business_rule
title: "Escribe aquí el título"
description: "Una frase sobre para qué sirve este documento."
tags: [auth, internal]
---

## Tu contenido

Este es el cuerpo del documento, en Markdown. El asistente lo recupera por estos términos y lo
usa como contexto, así que escribe lo que el equipo necesita saber, no lo que un tercero
necesitaría.
`

export function OkfEditorModal({ onClose, onSave, isSaving, saveFailed }: OkfEditorModalProps) {
  const { t } = useTranslation('knowledge')
  const [pestana, setPestana] = useState<Pestana>('editor')
  const [bruto, setBruto] = useState(PLANTILLA)
  const [tipo, setTipo] = useState<OkfType>('concept')
  const [titulo, setTitulo] = useState('')
  const [descripcion, setDescripcion] = useState('')
  const [etiquetas, setEtiquetas] = useState('')
  const [cuerpo, setCuerpo] = useState('')

  // El contenido que se envia es el de la pestaña activa, y los problemas se calculan sobre el
  // mismo. Se derivan durante el render en vez de guardarse en un estado que hay que mantener
  // sincronizado con el texto: un estado `problemas` se queda viejo en cuanto el usuario corrige
  // algo y el boton se desbloquea un evento tarde.
  const contenido = useMemo(
    () =>
      pestana === 'editor'
        ? bruto
        : componerOkf({
            type: tipo,
            // Los **nombres de los estados**, en español, y no los del campo que compone la
            // función. Escribir `title` aquí compilaría si existiera una variable con ese
            // nombre en el ambito, y no la hay: el resultado sería que el campo de título no
            // actualiza el documento y el botón de guardar se queda bloqueado sin motivo
            // visible. El aviso del linter sobre las dependencias sin usar lo habría delatado.
            title: titulo,
            description: descripcion,
            tags: etiquetas
              .split(',')
              .map((e) => e.trim())
              .filter((e) => e.length > 0),
            body: cuerpo,
          }),
    [bruto, cuerpo, descripcion, etiquetas, pestana, titulo, tipo],
  )

  const problemas = useMemo(() => validarOkf(contenido), [contenido])
  const esValido = problemas.length === 0

  function cambiarAEditor(): void {
    // Al pasar de formulario a editor se compone el documento, para que quien edite a mano
    // vea exactamente lo que el formulario iba a enviar. Sin esto, el editor abriria en
    // blanco y el usuario pensaria que ha perdido lo que habia escrito.
    if (pestana === 'formulario') {
      setBruto(contenido)
    }
    setPestana('editor')
  }

  function cambiarAFormulario(): void {
    setPestana('formulario')
    // Se rellena desde el documento que haya, para no perder lo escrito a mano.
    const extraido = extraerParaFormulario(bruto)
    setTipo(extraido.type)
    setTitulo(extraido.title)
    setDescripcion(extraido.description)
    setEtiquetas(extraido.tags.join(', '))
    setCuerpo(extraido.body)
  }

  async function guardar(): Promise<void> {
    if (!esValido || isSaving) return
    const extraido = extraerParaFormulario(contenido)
    const ok = await onSave({
      title: extraido.title || titulo,
      doc_type: TIPO_OKF_A_ENUM[extraido.type] as KnowledgeDocType,
      content: contenido,
    })
    if (ok) onClose()
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal okf-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="okf-modal-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <h2 id="okf-modal-title">
            <FileCode2 size={18} aria-hidden="true" />
            {t('okfEditor.title')}
          </h2>
          <button className="icon-button" type="button" onClick={onClose} aria-label={t('okfEditor.close')}>
            <X size={17} aria-hidden="true" />
          </button>
        </header>

        <div className="okf-tabs" role="tablist" aria-label={t('okfEditor.tabsLabel')}>
          <button
            className={pestana === 'editor' ? 'okf-tab okf-tab-active' : 'okf-tab'}
            type="button"
            role="tab"
            aria-selected={pestana === 'editor'}
            onClick={cambiarAEditor}
          >
            <PenLine size={14} aria-hidden="true" />
            {t('okfEditor.tabEditor')}
          </button>
          <button
            className={pestana === 'formulario' ? 'okf-tab okf-tab-active' : 'okf-tab'}
            type="button"
            role="tab"
            aria-selected={pestana === 'formulario'}
            onClick={cambiarAFormulario}
          >
            <FileCode2 size={14} aria-hidden="true" />
            {t('okfEditor.tabForm')}
          </button>
        </div>

        {pestana === 'editor' ? (
          <div className="okf-editor-pane">
            <label className="visually-hidden" htmlFor="okf-raw">
              {t('okfEditor.editorLabel')}
            </label>
            <textarea
              id="okf-raw"
              className="okf-textarea mono"
              value={bruto}
              spellCheck={false}
              onChange={(event) => setBruto(event.target.value)}
              placeholder={t('okfEditor.editorPlaceholder')}
            />
          </div>
        ) : (
          <div className="okf-form-pane">
            <div className="okf-form-row">
              <label className="field">
                <span className="field-label">{t('okfEditor.fieldTitle')}</span>
                <input
                  className="okf-input"
                  value={titulo}
                  onChange={(event) => setTitulo(event.target.value)}
                />
              </label>
              <label className="field">
                <span>{t('okfEditor.fieldType')}</span>
                <select
                  className="okf-input"
                  value={tipo}
                  onChange={(event) => setTipo(event.target.value as OkfType)}
                >
                  {OKF_TYPES.map((valor) => (
                    <option key={valor} value={valor}>
                      {t(`okfEditor.types.${valor}`)}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <label className="field">
              <span>{t('okfEditor.fieldDescription')}</span>
              <input
                className="okf-input"
                value={descripcion}
                onChange={(event) => setDescripcion(event.target.value)}
              />
            </label>

            <label className="field">
              <span>{t('okfEditor.fieldTags')}</span>
              <input
                className="okf-input"
                value={etiquetas}
                placeholder={t('okfEditor.tagsPlaceholder')}
                onChange={(event) => setEtiquetas(event.target.value)}
              />
            </label>

            <label className="field">
              <span>{t('okfEditor.fieldBody')}</span>
              <textarea
                className="okf-textarea"
                value={cuerpo}
                onChange={(event) => setCuerpo(event.target.value)}
              />
            </label>

            <details className="okf-preview">
              <summary>{t('okfEditor.preview')}</summary>
              <pre className="okf-preview-body mono">{contenido}</pre>
            </details>
          </div>
        )}

        {/* La validación vive **debajo** de las pestañas y no dentro de cada una, para que se vea
            igual en las dos formas de escribir. Reescribir el mismo bloque de avisos en dos
            sitios es la forma de que uno de los dos se quede sin actualizar. */}
        <div className="okf-validation" role="status" aria-live="polite">
          {esValido ? (
            <p className="okf-validation-ok">{t('okfEditor.valid')}</p>
          ) : (
            <ul className="okf-validation-list">
              {problemas.map((problema, indice) => (
                <li key={`${problema.campo}-${indice}`}>
                  <span className="okf-validation-campo">{problema.campo}</span>
                  <span>{t(problema.mensaje)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <footer className="modal-footer">
          {saveFailed && (
            <p className="okf-save-error" role="alert">
              {t('okfEditor.saveFailed')}
            </p>
          )}
          <button className="secondary-button" type="button" onClick={onClose}>
            {t('okfEditor.cancel')}
          </button>
          <button
            className="primary-button"
            type="button"
            onClick={() => void guardar()}
            disabled={!esValido || isSaving}
          >
            <Save size={16} aria-hidden="true" />
            {isSaving ? t('okfEditor.saving') : t('okfEditor.save')}
          </button>
        </footer>
      </div>
    </div>
  )
}
