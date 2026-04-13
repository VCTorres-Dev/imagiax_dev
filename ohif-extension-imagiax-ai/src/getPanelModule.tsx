import React from 'react';

import ImagiaxAiPanel from './ImagiaxAiPanel';

export default function getPanelModule({ servicesManager }: withAppTypes) {
  const wrappedImagiaxAiPanel = () => {
    return <ImagiaxAiPanel servicesManager={servicesManager} />;
  };

  return [
    {
      name: 'imagiaxAiPanel',
      iconName: 'tab-linear',
      iconLabel: 'IMAGIAX IA',
      label: 'IMAGIAX IA',
      component: wrappedImagiaxAiPanel,
    },
  ];
}
