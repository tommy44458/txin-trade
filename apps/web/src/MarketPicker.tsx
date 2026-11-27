import { uiText } from "./i18n/index.ts";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import Icon from "./Icon";
import "./MarketPicker.css";

export type Market = {
  id: string;
  symbol: string;
  exchange: string;
  base_asset?: string;
  quote_asset?: string;
  settlement_asset?: string;
  contract_type?: string;
  asset_class?: "crypto" | "us_equity";
};

type AssetFilter = "all" | "crypto" | "us_equity";

function searchKey(value: string) {
  return value.toUpperCase().replace(/[^\p{L}\p{N}]/gu, "");
}

function fallbackSymbol(id: string) {
  return id.split(":").at(-1)?.replace(/(USDT|USDC)$/, "/$1") ?? id;
}

export default function MarketPicker({
  markets,
  value,
  onChange,
  favoriteIds,
  onToggleFavorite,
  favoritesReady,
  favoritesSaving,
  favoritesError,
  label = uiText("交易對"),
  emptyLabel = uiText("尚無交易對"),
}: {
  markets: Market[];
  value: string;
  onChange: (id: string) => void;
  favoriteIds: string[];
  onToggleFavorite: (id: string) => void;
  favoritesReady: boolean;
  favoritesSaving: boolean;
  favoritesError: string;
  label?: string;
  emptyLabel?: string;
}) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [favoriteOnly, setFavoriteOnly] = useState(false);
  const [assetFilter, setAssetFilter] = useState<AssetFilter>("all");
  const [limit, setLimit] = useState(100);
  const [activeId, setActiveId] = useState("");
  const [placement, setPlacement] = useState({
    left: 12,
    top: 100,
    width: 360,
    maxHeight: 480,
  });
  const selected = markets.find((market) => market.id === value);
  const selectedSymbol = selected?.symbol ?? (value ? fallbackSymbol(value) : emptyLabel);
  const favoriteSet = useMemo(() => new Set(favoriteIds), [favoriteIds]);
  const filtered = useMemo(() => {
    const key = searchKey(query);
    return markets
      .filter(
        (market) =>
          (!favoriteOnly || favoriteSet.has(market.id)) &&
          (assetFilter === "all" || (market.asset_class ?? "crypto") === assetFilter) &&
          (!key ||
            searchKey(market.symbol).includes(key) ||
            searchKey(market.id.split(":").at(-1) ?? "").includes(key)),
      )
      .sort((a, b) => {
        const pinned = Number(favoriteSet.has(b.id)) - Number(favoriteSet.has(a.id));
        return pinned || a.symbol.localeCompare(b.symbol, "en");
      });
  }, [markets, favoriteOnly, assetFilter, favoriteSet, query]);
  const visible = filtered.slice(0, limit);
  const activeIndex = visible.findIndex((market) => market.id === activeId);
  const effectiveActiveId = activeIndex >= 0 ? activeId : visible[0]?.id;
  const pinned = favoriteIds
    .map((favorite) => markets.find((market) => market.id === favorite))
    .filter((market): market is Market => !!market);

  function close(restoreFocus = false) {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus({ preventScroll: true });
  }

  function choose(marketId: string) {
    onChange(marketId);
    close(true);
  }

  function show(favorites = false) {
    setQuery("");
    setFavoriteOnly(favorites);
    setLimit(100);
    setActiveId(value);
    setOpen(true);
  }

  useEffect(() => {
    if (!open) return;
    function reposition() {
      const rect = trigger.current?.getBoundingClientRect();
      if (!rect) return;
      const width = Math.min(380, window.innerWidth - 24);
      const below = window.innerHeight - rect.bottom - 20;
      const above = rect.top - 20;
      const useBelow = below >= Math.min(300, above);
      const maxHeight = Math.min(480, useBelow ? below : above);
      setPlacement({
        left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)),
        top: useBelow ? rect.bottom + 8 : Math.max(12, rect.top - maxHeight - 8),
        width,
        maxHeight: Math.max(180, maxHeight),
      });
    }
    function outside(event: PointerEvent) {
      if (
        !popup.current?.contains(event.target as Node) &&
        !trigger.current?.contains(event.target as Node)
      ) close();
    }
    reposition();
    input.current?.focus({ preventScroll: true });
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    document.addEventListener("pointerdown", outside);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
      document.removeEventListener("pointerdown", outside);
    };
  }, [open]);

  useEffect(() => {
    if (!open || !effectiveActiveId) return;
    // Only move the popup's list; scrollIntoView can also move the document.
    const item = document.getElementById(`${id}-${effectiveActiveId}`);
    const list = popup.current?.querySelector<HTMLElement>(".market-picker-results");
    if (!item || !list) return;
    const row = item.getBoundingClientRect();
    const viewport = list.getBoundingClientRect();
    if (row.top < viewport.top) list.scrollTop += row.top - viewport.top;
    else if (row.bottom > viewport.bottom) list.scrollTop += row.bottom - viewport.bottom;
  }, [open, effectiveActiveId, id]);

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const current = visible.findIndex((market) => market.id === effectiveActiveId);
      const next = event.key === "ArrowDown"
        ? Math.min(current + 1, visible.length - 1)
        : Math.max(current - 1, 0);
      if (visible[next]) setActiveId(visible[next].id);
    } else if (event.key === "Enter" && effectiveActiveId) {
      event.preventDefault();
      choose(effectiveActiveId);
    }
  }

  return (
    <div className="market-picker-field">
      <span className="control-label" id={`${id}-label`}>{label}</span>
      <div className="market-picker">
        <button
          type="button"
          ref={trigger}
          className="market-picker-trigger"
          aria-labelledby={`${id}-label ${id}-value`}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-controls={open ? `${id}-dialog` : undefined}
          disabled={!markets.length}
          onClick={() => open ? close() : show()}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault();
              show();
            }
          }}
        >
          <span id={`${id}-value`}>{selectedSymbol}</span>
          <Icon name="chevronDown" />
        </button>
        <button
          type="button"
          className={`market-favorite-toggle ${favoriteSet.has(value) ? "is-favorite" : ""}`}
          aria-label={uiText("{{p0}}最愛 {{p1}}", { p0: favoriteSet.has(value) ? uiText("移除") : uiText("加入"), p1: selectedSymbol })}
          aria-pressed={favoriteSet.has(value)}
          disabled={!favoritesReady || !selected}
          onClick={() => onToggleFavorite(value)}
          title={favoriteSet.has(value) ? uiText("移除最愛") : uiText("加入最愛")}
        >
          <Icon name="star" />
        </button>
      </div>
      {favoritesError && <p className="market-picker-error" role="alert">{favoritesError}</p>}
      <span className="sr-only" role="status">{favoritesSaving ? uiText("正在儲存最愛交易對") : ""}</span>
      {open && createPortal(
        <div
          className="market-picker-popup"
          id={`${id}-dialog`}
          role="dialog"
          aria-label={uiText("選擇交易對")}
          ref={popup}
          style={placement}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.stopPropagation();
              close(true);
            }
          }}
          onBlur={(event) => {
            if (
              event.relatedTarget &&
              !event.currentTarget.contains(event.relatedTarget) &&
              !trigger.current?.contains(event.relatedTarget)
            ) close();
          }}
        >
          <div className="market-picker-search">
            <Icon name="search" />
            <input
              ref={input}
              role="combobox"
              aria-label={uiText("搜尋交易對")}
              aria-autocomplete="list"
              aria-controls={`${id}-list`}
              aria-expanded={open}
              aria-activedescendant={effectiveActiveId ? `${id}-${effectiveActiveId}` : undefined}
              autoComplete="off"
              spellCheck={false}
              placeholder={uiText("搜尋 BTC、SOL、TSLA 或交易對")}
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setActiveId("");
                setLimit(100);
              }}
              onKeyDown={onKeyDown}
            />
            <button type="button" className="market-picker-dismiss" aria-label={uiText("關閉交易對選單")} onClick={() => close(true)}>
              <Icon name="close" />
            </button>
          </div>
          <div className="market-picker-filters">
            <div role="group" aria-label={uiText("交易對清單")}>
              <button type="button" aria-pressed={!favoriteOnly} onClick={() => { setFavoriteOnly(false); setLimit(100); }}>{uiText("全部")}</button>
              <button type="button" aria-pressed={favoriteOnly} onClick={() => { setFavoriteOnly(true); setLimit(100); }}>{uiText("最愛")}{" "}{pinned.length > 0 ? ` ${pinned.length}` : ""}</button>
            </div>
            {markets.some((market) => market.asset_class === "us_equity") && (
              <div role="group" aria-label={uiText("資產類別")}>
                {(["all", "crypto", "us_equity"] as const).map((filter) => (
                  <button type="button" key={filter} aria-pressed={assetFilter === filter}
                    onClick={() => { setAssetFilter(filter); setLimit(100); }}>
                    {filter === "all" ? uiText("所有類別") : filter === "crypto" ? uiText("加密貨幣") : uiText("美股")}
                  </button>
                ))}
              </div>
            )}
          </div>
          <div className="market-picker-results">
            <div id={`${id}-list`} role="listbox" aria-label={uiText("合約交易對")}>
              {visible.map((market, index) => {
                const favorite = favoriteSet.has(market.id);
                const groupStart = index === 0 || favorite !== favoriteSet.has(visible[index - 1].id);
                return (
                  <div key={market.id} role="presentation">
                    {groupStart && <p className="market-picker-group" role="presentation">{favorite ? uiText("最愛") : uiText("所有交易對")}</p>}
                    <div className={`market-picker-row ${market.id === effectiveActiveId ? "is-active" : ""}`} role="presentation">
                      <button
                        type="button"
                        id={`${id}-${market.id}`}
                        role="option"
                        aria-selected={market.id === value}
                        className="market-picker-option"
                        onClick={() => choose(market.id)}
                      >
                        <span>{market.symbol}</span>
                        <small>{market.id === value ? uiText("已選擇")
                          : market.asset_class === "us_equity" ? uiText("美股永續") : uiText("永續")}</small>
                      </button>
                      <button
                        type="button"
                        className={`market-favorite-toggle ${favorite ? "is-favorite" : ""}`}
                        aria-label={uiText("{{p0}}最愛 {{p1}}", { p0: favorite ? uiText("移除") : uiText("加入"), p1: market.symbol })}
                        aria-pressed={favorite}
                        disabled={!favoritesReady}
                        onClick={() => onToggleFavorite(market.id)}
                      ><Icon name="star" /></button>
                    </div>
                  </div>
                );
              })}
            </div>
            {visible.length === 0 && <p className="market-picker-empty">{favoriteOnly && !query ? uiText("點選交易對旁的星號，加入最愛。") : markets.length === 0 ? uiText("交易對清單尚未載入。") : uiText("找不到符合的交易對，請更換關鍵字。")}</p>}{" "}
            {filtered.length > limit && <button type="button" className="market-picker-more" onClick={() => setLimit(limit + 100)}>{uiText("顯示更多交易對")}</button>}
          </div>
          <div className="market-picker-footer"><span>{filtered.length}{" " + uiText("個交易對")}</span><span>{uiText("↑ ↓ 選擇 · Enter 確認")}</span></div>
        </div>, document.body,
      )}
    </div>
  );
}
