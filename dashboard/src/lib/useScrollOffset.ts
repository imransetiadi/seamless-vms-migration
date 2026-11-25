import { useLayoutEffect, useRef } from 'react';

/** Publish the rendered height of sticky navigation for fragment jumps and keyboard scrolling. */
export function useScrollOffset<T extends HTMLElement>(property: '--mobile-header-height' | '--section-nav-height') {
  const ref = useRef<T>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const style = document.documentElement.style;
    const update = () => style.setProperty(property, `${element.getBoundingClientRect().height}px`);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => {
      observer.disconnect();
      style.removeProperty(property);
    };
  }, [property]);
  return ref;
}
