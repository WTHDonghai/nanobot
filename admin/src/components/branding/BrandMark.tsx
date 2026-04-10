import React from 'react';
import companyLogo from '../../assets/company-logo.png';
import './BrandMark.css';

type BrandMarkProps = {
  size?: 'sm' | 'md' | 'lg';
  className?: string;
  alt?: string;
};

const BrandMark: React.FC<BrandMarkProps> = ({
  size = 'md',
  className = '',
  alt = 'Company logo',
}) => {
  const classes = ['brand-mark', `brand-mark--${size}`, className].filter(Boolean).join(' ');

  return <img src={companyLogo} alt={alt} className={classes} draggable={false} />;
};

export default BrandMark;
